import asyncio
import json
import logging
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

    def disconnect(self, websocket: WebSocket):
        """處理斷線，並更新用戶數。"""
        self.active_connections.discard(websocket)
        self.update_user_count()

    def update_user_count(self):
        """更新共享數據中的線上用戶數。"""
        self.main_json_data["users_online"] = len(self.active_connections)
    
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

        # 數據同步：推送最新的數據
        sync_message = json.dumps({
            "type": "data_sync",
            "payload": manager.main_json_data 
        })
        await websocket.send_text(sync_message)
        logger.info("已同步數據給新連線")

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
            
            if message_type == "UPDATE_FROM_A":
                # 更新伺服器端的主 JSON
                update_payload = data.get("payload", {})
                
                manager.main_json_data.update({
                    "last_updated": datetime.now().isoformat(),
                    "users_online": len(manager.active_connections), 
                    "custom_data": update_payload 
                })
                
                logger.info(f"收到 A 的主數據更新")
                
                # 廣播更新
                broadcast_message = json.dumps({
                    "type": "data_update",
                    "payload": manager.main_json_data 
                })
                
                await manager.broadcast(broadcast_message)
            
            elif message_type == "MESSAGE_FROM_A":
                chat_message = data.get("content", "無內容")
                logger.info(f"收到 A 的即時訊息：{chat_message}")

                await manager.broadcast(json.dumps({
                    "type": "chat_message",
                    "content": chat_message
                }))

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