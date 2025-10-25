import asyncio
import json
import logging
from datetime import datetime
import os

# 導入 FastAPI 和相關模組
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
# 導入 starlette 的 WebSocketState 用於連線檢查
from starlette.websockets import WebSocketState


# 配置日誌記錄，包含時間戳和等級
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# ----------------------------------------------
# 1. 連線管理類別 (Connection Manager)
#    - 封裝連線集合和共享數據，避免 NameError
# ----------------------------------------------

class ConnectionManager:
    """以單例模式管理所有活動連線及其共享數據。"""
    
    def __init__(self):
        # 將連線集合和資料作為類別屬性
        self.active_connections: set[WebSocket] = set()
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
        
        # 遍歷所有已連線的客戶端
        for client in self.active_connections:
            # 檢查 Starlette/FastAPI WebSocket 的狀態
            if client.client_state == WebSocketState.CONNECTED:
                try:
                    await client.send_text(message)
                except Exception as e:
                    logger.error(f"廣播時發生錯誤: {e}")
                    clients_to_remove.add(client)
            else:
                 clients_to_remove.add(client)

        # 安全地從連線集合中移除斷線的客戶端
        for client in clients_to_remove:
            self.active_connections.discard(client)
        
        if clients_to_remove:
            logger.info(f"已移除 {len(clients_to_remove)} 個斷線客戶端。當前連線數: {len(self.active_connections)}")

# 【創建一個應用程式單例實例，負責連線和數據管理】
manager = ConnectionManager()


# ----------------------------------------------
# 2. FastAPI 應用程式實例
# ----------------------------------------------
app = FastAPI() 


# ----------------------------------------------
# 3. WebSocket 路由
# ----------------------------------------------

@app.websocket("/ws") # 客戶端將連線到 wss://your-service.onrender.com/ws
async def fastapi_websocket_endpoint(websocket: WebSocket):
    
    # 1. 註冊連線
    try:
        await manager.connect(websocket)
        logger.info(f"新客戶端連線。當前連線數: {len(manager.active_connections)}")

        # 數據同步：客戶端連線後，立即推送最新的數據
        sync_message = json.dumps({
            "type": "data_sync",
            "payload": manager.main_json_data 
        })
        await websocket.send_text(sync_message)
        logger.info("已同步數據給新連線")

        # 2. 處理接收到的訊息
        while True:
            # 使用 FastAPI/Starlette 的 receive_text()
            message = await websocket.receive_text()
            
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                logger.warning(f"接收到非 JSON 訊息，忽略。")