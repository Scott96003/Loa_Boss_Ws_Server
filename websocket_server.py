import asyncio
import json
import logging
from typing import Dict, List
from datetime import datetime
# 導入 FastAPI 和相關模組
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from starlette.middleware.cors import CORSMiddleware # 新增：為了部署到 Render

# 配置日誌記錄，包含時間戳和等級
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ----------------------------------------------
# 1. 連線管理類別 (Connection Manager)
# ----------------------------------------------

class ConnectionManager:
    """以單例模式管理所有活動連線及其共享數據。"""
    
    def __init__(self):
        self.active_connections: set[WebSocket] = set()
        self.user_to_ws: Dict[str, WebSocket] = {} # Map<ID, WebSocket>
        self.ws_to_user: Dict[WebSocket, str] = {} # Map<WebSocket, ID> - 反向查找

        self.main_json_data = {
            "status": "Offline",
            "users_online": 0,
            "last_updated": datetime.now().isoformat(),
            "custom_data": {}
        }
        
    async def connect(self, websocket: WebSocket):
        """處理新連線，並更新用戶數。"""
        await websocket.accept()
        self.active_connections.add(websocket)
        self.update_user_count()

    def register_user(self, user_id: str, websocket: WebSocket):
        """將連線與其唯一的客戶端 ID 關聯。"""
        # 確保只在 ID 不存在時註冊，防止覆蓋
        if user_id not in self.user_to_ws:
            self.user_to_ws[user_id] = websocket
            self.ws_to_user[websocket] = user_id
            logger.info(f"用戶 ID '{user_id}' 已註冊。")

            # 🔥 關鍵：如果確實是新用戶，廣播通知所有其他人
            asyncio.create_task(self.broadcast_user_joined(user_id))
        else:
            logger.warning(f"用戶 ID '{user_id}' 已存在，跳過註冊。")

    async def broadcast_user_joined(self, new_user_id: str):
        """通知所有在線用戶有新 ID 上線。"""
        message = {
            "type": "user_joined", # 新的信令類型
            "senderId": "server",
            "targetId": "all",
            "newUserId": new_user_id
        }
        json_message = json.dumps(message)
        
        # 廣播給所有已經註冊的用戶（除了新加入的用戶自己）
        for user_id, ws in self.user_to_ws.items():
            if user_id != new_user_id:
                try:
                    await ws.send_text(json_message)
                except Exception as e:
                    logger.error(f"通知 {user_id} 新用戶上線失敗: {e}")

    def disconnect(self, websocket: WebSocket):
        """處理斷線，並更新用戶數。"""
        self.active_connections.discard(websocket)
        
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
        # 檢查是否連線，且狀態為連接中
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
            if client.client_state == WebSocketState.CONNECTED:
                try:
                    await client.send_text(message) 
                except Exception as e:
                    logger.error(f"廣播時發生錯誤: {e}")
                    clients_to_remove.add(client)
            else:
                 clients_to_remove.add(client)

        for client in clients_to_remove:
            self.disconnect(client) # 使用 disconnect 函數來處理所有清理工作
        
        if clients_to_remove:
            logger.info(f"已移除 {len(clients_to_remove)} 個斷線客戶端。當前連線數: {len(self.active_connections)}")

manager = ConnectionManager()


# ----------------------------------------------
# 2. FastAPI 應用程式實例
# ----------------------------------------------
app = FastAPI() 

# ⚠️ 部署所需的 CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------
# 3. WebSocket 路由 - 修正版
# ----------------------------------------------

@app.websocket("/ws") # 路由路徑
async def fastapi_websocket_endpoint(websocket: WebSocket):
    
    current_user_id = None 

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
            sender_id = data.get("senderId")
            target_id = data.get("targetId") # 提前取得

            
            # --- 【步驟 A：檢查並設置 current_user_id】 ---
            # 確保連線的 current_user_id 與其 senderId 匹配
            if sender_id and sender_id in manager.user_to_ws:
                current_user_id = sender_id
            
            # --- 【步驟 B：處理信令或指令】 ---

            if message_type == 'client_register':
                # 處理客戶端註冊訊息
                if sender_id and sender_id not in manager.user_to_ws:
                    manager.register_user(sender_id, websocket)
                    current_user_id = sender_id
                    logger.info(f"✅ 成功處理客戶端註冊：{sender_id}")
                else:
                    logger.warning(f"客戶端註冊訊息重複或無效：{sender_id}")
            
            elif message_type in ['offer','answer','candidate','chat_message']:
                # 處理 WebRTC 信令與 P2P 訊息
                if not current_user_id:
                    logger.warning(f"[P2P 信令] 收到 {message_type} 但發送方 ID 未註冊，跳過。")
                    continue
                    
                if target_id:
                    # 點對點轉發給目標用戶
                    success = await manager.send_personal_message(message, target_id)
                    log_action = "成功轉發" if success else "轉發失敗"
                    logger.info(f"[P2P 信令] {sender_id} -> {target_id}: {message_type}. {log_action}.")
                else:
                    logger.warning(f"[P2P 信令] 收到信令但缺少 targetId: {message_type}")

            elif message_type in ["Sync_Boss_Data", "Boss_Death", "Ack_Sync"]:
                # 處理遊戲廣播訊息
                json_string_to_broadcast = json.dumps(data)
                
                logger.info(f"廣播訊息類型: {message_type}")
                await manager.broadcast(json_string_to_broadcast)
                
            elif message_type == 'request_online_users':
                # 處理請求在線用戶列表
                online_users = manager.get_online_users()
                
                response = {
                    "type": "online_users_list",
                    "users": online_users,
                    "senderId": "server"
                }
                
                target_id_for_response = current_user_id if current_user_id else sender_id
                if target_id_for_response:
                    await manager.send_personal_message(json.dumps(response), target_id_for_response)
                    logger.info(f"📢 已將 {len(online_users)} 個用戶 ID 列表回傳給 {target_id_for_response}")
                else:
                    logger.error("🚫 無法回覆在線用戶列表：目標 ID 不明。")
                
            else:
                logger.warning(f"收到未知訊息類型: {message_type}") # 將 INFO 改為 WARNING


    except WebSocketDisconnect:
        logger.info("客戶端關閉連線。")
        # 斷線時，清理 active_connections 和 user/ws maps
    except Exception as e:
        logger.error(f"連線錯誤：{e}")
        # 發生其他錯誤時
    finally:
        # 3. 移除連線 (這裡會處理 active_connections, user_to_ws, ws_to_user 的移除)
        manager.disconnect(websocket)
        logger.info(f"客戶端已斷開。當前連線數: {len(manager.active_connections)}")