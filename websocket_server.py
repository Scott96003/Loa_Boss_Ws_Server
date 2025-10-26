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

# 創建一個單例實例，在應用程式的生命週期內管理連線和數據
# ----------------------------------------------
# 1. 連線管理類別 (Connection Manager) - 修正版
# ----------------------------------------------

class ConnectionManager:
    """以單例模式管理所有活動連線及其共享數據。"""
    
    def __init__(self):
        self.active_connections: set[WebSocket] = set()
        self.user_to_ws: Dict[str, WebSocket] = {} # Map<ID, WebSocket>

        # 🚨 修正 #1.1：移除 ws_to_user 的初始化，因為我們不需要它，或者必須新增。
        # 為了保持您的邏輯結構，我將它加回來，但建議只用 user_to_ws
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
        # 🚨 修正 #1.2：確保只在 ID 不存在時註冊，防止覆蓋
        if user_id not in self.user_to_ws:
            self.user_to_ws[user_id] = websocket
            self.ws_to_user[websocket] = user_id
            logger.info(f"用戶 ID '{user_id}' 已註冊。")
        else:
            logger.warning(f"用戶 ID '{user_id}' 已存在，跳過註冊。")

        
    def disconnect(self, websocket: WebSocket):
        """處理斷線，並更新用戶數。"""
        self.active_connections.discard(websocket)
        
        # 🚨 修正 #1.3：處理斷線邏輯：從 ws_to_user 移除後，再從 user_to_ws 移除
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
    
    current_user_id = None # 🚨 修正 #2：確保 current_user_id 在 try 塊之外被初始化

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
            
            # --- 【步驟 A：首次連線時註冊 ID】 ---
            # 客戶端第一次發送 Offer/Answer 或任何帶有 senderId 的信令時進行註冊
            if sender_id and sender_id not in manager.user_to_ws:
                manager.register_user(sender_id, websocket)
                current_user_id = sender_id # 註冊成功後，設置當前連線的 ID

            # 如果已經註冊，也更新 current_user_id，以便斷線時使用
            if sender_id in manager.user_to_ws:
                current_user_id = sender_id


            # --- 【步驟 B：處理信令或指令】 ---
            if message_type in ["Sync_Boss_Data", "Boss_Death", "Ack_Sync"]:
                # 將 Python 字典序列化為 JSON 字串，以便傳輸
                json_string_to_broadcast = json.dumps(data)
                
                logger.info(f"廣播訊息類型: {message_type}")
                await manager.broadcast(json_string_to_broadcast)
            
            elif message_type in ['offer','answer','candidate','chat_message']:
                target_id = data.get("targetId") # 接收目標 ID
                    
                # 確保有目標 ID
                if target_id:
                    # 點對點轉發給目標用戶
                    success = await manager.send_personal_message(message, target_id)
                    log_action = "成功轉發" if success else "轉發失敗"
                    logger.info(f"[P2P 信令] {sender_id} -> {target_id}: {message_type}. {log_action}.")
                else:
                    logger.warning(f"[P2P 信令] 收到信令但缺少 targetId: {message_type}")
                
            elif message_type == 'request_online_users':
                # 取得所有用戶 ID
                online_users = manager.get_online_users()
                
                # 建立回覆訊息
                response = {
                    "type": "online_users_list",
                    "users": online_users,
                    "senderId": "server"
                }
                
                # 將列表發送回給請求者 (使用 current_user_id 或 sender_id)
                target_id_for_response = current_user_id if current_user_id else sender_id
                if target_id_for_response:
                    await manager.send_personal_message(json.dumps(response), target_id_for_response)
                    logger.info(f"📢 已將 {len(online_users)} 個用戶 ID 列表回傳給 {target_id_for_response}")
                else:
                    logger.error("🚫 無法回覆在線用戶列表：目標 ID 不明。")
                
            else:
                logger.info(f"收到未知訊息類型: {message_type}")


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