import asyncio
import json
import logging
from typing import Dict, List  # <--- 在這裡匯入 List！
from datetime import datetime
# 導入 FastAPI 和相關模組
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState


# 配置日誌記錄，包含時間戳和等級
# 注意：在生產環境，日誌級別應設定為 INFO 或 WARNING
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ----------------------------------------------
# 1. 連線管理類別 (Connection Manager)
#    - 封裝連線集合和共享數據，徹底解決 NameError
# ----------------------------------------------

class ConnectionManager:
    """以單例模式管理所有活動連線及其共享數據。"""
    
    def __init__(self):
        # 連線集合，現在是類別屬性
        self.active_connections: set[WebSocket] = set()
        self.user_to_ws: dict[str, WebSocket] = {} # 新增：ID 映射到 WebSocket

        # 共享數據，現在是類別屬性
        self.main_json_data = {
            "status": "Offline",
            "users_online": 0,
            "last_updated": datetime.now().isoformat(),
            "custom_data": {}
        }
        
    async def connect(self, websocket: WebSocket):
        """處理新連線，並更新用戶數。"""
        # 必須先接受連線
        await websocket.accept()
        self.active_connections.add(websocket)
        self.update_user_count()

    def register_user(self, user_id: str, websocket: WebSocket):
        """將連線與其唯一的客戶端 ID 關聯。"""
        self.user_to_ws[user_id] = websocket
        self.ws_to_user[websocket] = user_id
        logger.info(f"用戶 ID '{user_id}' 已註冊。")
        
    def disconnect(self, websocket: WebSocket):
        """處理斷線，並更新用戶數。"""
        self.active_connections.discard(websocket)
        
        # 移除 ID 映射
        user_id = self.ws_to_user.pop(websocket, None)
        if user_id:
            self.user_to_ws.pop(user_id, None)
            logger.info(f"用戶 ID '{user_id}' 已移除。")
        
        self.update_user_count()

    def update_user_count(self):
        """更新共享數據中的線上用戶數。"""
        self.main_json_data["users_online"] = len(self.active_connections)

    def get_online_users(self) -> List[str]:
        """返回所有在線用戶的 ID 列表。"""
        return list(self.user_to_ws.keys())

    # 新增：點對點傳輸方法
    async def send_personal_message(self, message: str, user_id: str) -> bool:
        """將訊息傳送給特定的客戶端 ID。"""
        client = self.user_to_ws.get(user_id)
        if client and client.client_state == WebSocketState.CONNECTED:
            try:
                await client.send_text(message)
                return True
            except Exception as e:
                logger.error(f"傳送訊息給 {user_id} 時發生錯誤: {e}")
                # 如果傳輸失敗，視為斷線處理
                self.disconnect(client) 
                return False
        
        logger.warning(f"用戶 ID '{user_id}' 不在線或未註冊。無法傳送訊息。")
        return False

    async def broadcast(self, message: str):
        """將訊息廣播給所有已連線的客戶端，並安全地處理斷線錯誤。"""
        clients_to_remove = set() 
        
        for client in self.active_connections:
            # 檢查 WebSocket 的狀態
            if client.client_state == WebSocketState.CONNECTED:
                try:
                    # 使用 FastAPI 的 send_text
                    await client.send_text(message) 
                except Exception as e:
                    logger.error(f"廣播時發生錯誤: {e}")
                    clients_to_remove.add(client)
            else:
                 clients_to_remove.add(client)

        for client in clients_to_remove:
            self.active_connections.discard(client)
        
        if clients_to_remove:
            logger.info(f"已移除 {len(clients_to_remove)} 個斷線客戶端。當前連線數: {len(self.active_connections)}")

# 創建一個單例實例，在應用程式的生命週期內管理連線和數據
manager = ConnectionManager()


# ----------------------------------------------
# 2. FastAPI 應用程式實例
# ----------------------------------------------
# Start Command 將會使用這個名為 'app' 的實例
app = FastAPI() 


# ----------------------------------------------
# 3. WebSocket 路由
# ----------------------------------------------

@app.websocket("/ws") # 路由路徑
async def fastapi_websocket_endpoint(websocket: WebSocket):
    
    # 這裡不再需要 'global' 關鍵字
    
    try:
        # 1. 註冊連線
        await manager.connect(websocket)
        logger.info(f"新客戶端連線。當前連線數: {len(manager.active_connections)}")

        # 2. 處理接收到的訊息
        while True:
            # 接收客戶端訊息
            message = await websocket.receive_text()
            
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                logger.warning(f"接收到非 JSON 訊息，忽略。")
                continue
            
            message_type = data.get("type")
            
            # 【關鍵修正：在廣播前進行 JSON 序列化】
            if message_type in ["Sync_Boss_Data", "Boss_Death", "Ack_Sync"]:
                
                # 將 Python 字典序列化為 JSON 字串，以便傳輸
                json_string_to_broadcast = json.dumps(data)
                
                logger.info(f"廣播訊息類型: {message_type}")
                await manager.broadcast(json_string_to_broadcast)
                continue # 跳過後續的

            elif message_type in ['offer','answer','candidate','chat_message']:
                sender_id = data.get("senderId")
                target_id = data.get("targetId") # 接收目標 ID

                # --- 【步驟 A：首次連線時註冊 ID】 ---
                if sender_id and websocket not in manager.ws_to_user:
                    manager.register_user(sender_id, websocket)
                    current_user_id = sender_id
                    logger.info(f"用戶 {sender_id} 完成註冊。")

                    
                # 確保有目標 ID
                if target_id:
                    # 點對點轉發給目標用戶
                    success = await manager.send_personal_message(message, target_id)
                    log_action = "成功轉發" if success else "轉發失敗"
                    logger.info(f"[P2P 信令] {sender_id} -> {target_id}: {message_type}. {log_action}.")
                else:
                    logger.warning(f"[P2P 信令] 收到信令但缺少 targetId: {message_type}")
                continue # 跳過後續的

            elif message_type == 'request_online_users':
                # 取得所有用戶 ID
                online_users = manager.get_online_users()
                
                # 建立回覆訊息
                response = {
                    "type": "online_users_list",
                    "users": online_users,
                    "senderId": "server" # 標記為伺服器發送
                }
                
                # 將列表發送回給請求者
                await manager.send_personal_message(json.dumps(response), client_id)
                logger.info(f"📢 已將 {len(online_users)} 個用戶 ID 列表回傳給 {client_id}")
                continue # 跳過後續的
                
            else:
                logger.info(f"收到未知訊息類型: {message_type}")


    except WebSocketDisconnect:
        logger.info("客戶端關閉連線。")
    except Exception as e:
        logger.error(f"連線錯誤：{e}")
    finally:
        # 3. 移除連線
        manager.disconnect(websocket)
        logger.info(f"客戶端已斷開。當前連線數: {len(manager.active_connections)}")