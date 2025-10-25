import asyncio
import websockets
import json
import logging
from datetime import datetime

# 配置日誌記錄，包含時間戳和等級
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ----------------------------------------------
# 1. 廣播核心變數
# ----------------------------------------------

# 用於追蹤所有已連線的客戶端
CONNECTED_CLIENTS = set()

# 儲存主要 JSON 數據（初始化結構）
MAIN_JSON_DATA = {
    "status": "Offline",
    "users_online": 0,
    "last_updated": datetime.now().isoformat(),
    "custom_data": {}
}

async def broadcast(message):
    """將訊息廣播給所有已連線的客戶端，並安全地處理斷線錯誤。"""
    
    clients_to_remove = set() 
    
    # 遍歷所有已連線的客戶端
    for client in CONNECTED_CLIENTS:
        try:
            # 嘗試傳送訊息
            await client.send(message)
        except websockets.exceptions.ConnectionClosed:
            # 如果連線已關閉，標記為移除
            clients_to_remove.add(client)
        except Exception as e:
            # 處理其他潛在的傳送錯誤
            logger.error(f"廣播時發生意外錯誤: {e}")
            clients_to_remove.add(client)

    # 安全地從連線集合中移除斷線的客戶端
    for client in clients_to_remove:
        CONNECTED_CLIENTS.discard(client)
    
    if clients_to_remove:
        logger.info(f"已移除 {len(clients_to_remove)} 個斷線客戶端。當前連線數: {len(CONNECTED_CLIENTS)}")


async def server_handler(websocket, path):
    """處理每個新的 WebSocket 連線。"""
    
    # 必須在使用 MAIN_JSON_DATA 變數前聲明它是 global
    global MAIN_JSON_DATA 
    
    # 1. 註冊連線
    CONNECTED_CLIENTS.add(websocket)
    # 連線後更新線上用戶數
    MAIN_JSON_DATA["users_online"] = len(CONNECTED_CLIENTS) 
    logger.info(f"新客戶端連線：{websocket.remote_address}。當前連線數: {len(CONNECTED_CLIENTS)}")

    try:
        # 數據同步：客戶端 B 連線時，立即推送最新的 MAIN_JSON_DATA
        sync_message = json.dumps({
            "type": "data_sync",
            "payload": MAIN_JSON_DATA 
        })
        await websocket.send(sync_message)
        logger.info(f"已同步數據給新連線：{websocket.remote_address}")

        # 2. 處理接收到的訊息
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                logger.warning(f"接收到非 JSON 訊息，忽略。")
                continue
            
            message_type = data.get("type")
            
            if message_type == "UPDATE_FROM_A":
                # 來自 Extension A 的主數據更新請求
                
                # 2.1 更新伺服器端的主 JSON
                update_payload = data.get("payload", {})
                
                # 【核心修正】：不使用 ** 解包，而是將整個 payload 賦值給 custom_data 鍵
                MAIN_JSON_DATA.update({
                    "last_updated": datetime.now().isoformat(),
                    "users_online": len(CONNECTED_CLIENTS), 
                    "custom_data": update_payload # <--- 修正點
                })
                
                logger.info(f"收到 A 的主數據更新，數據結構類型: {type(update_payload)}")
                
                # 2.2 廣播更新給所有訂閱者 B
                broadcast_message = json.dumps({
                    "type": "data_update",
                    "payload": MAIN_JSON_DATA 
                })
                
                await broadcast(broadcast_message)
            
            elif message_type == "MESSAGE_FROM_A":
                # 來自 Extension A 的即時聊天訊息
                chat_message = data.get("content", "無內容")
                
                logger.info(f"收到 A 的即時訊息：{chat_message}")

                # 廣播即時聊天訊息給所有 B
                await broadcast(json.dumps({
                    "type": "chat_message",
                    "content": chat_message
                }))

            else:
                logger.info(f"收到未知訊息類型: {message_type}")

    except websockets.exceptions.ConnectionClosedOK:
        logger.info(f"客戶端關閉連線：{websocket.remote_address}")
    except Exception as e:
        logger.error(f"連線錯誤：{e}")
    finally:
        # 3. 移除連線
        CONNECTED_CLIENTS.discard(websocket)
        # 更新線上用戶數
        MAIN_JSON_DATA["users_online"] = len(CONNECTED_CLIENTS) 
        logger.info(f"客戶端已斷開。當前連線數: {len(CONNECTED_CLIENTS)}")


# ----------------------------------------------
# 2. 伺服器啟動邏輯
# ----------------------------------------------

async def main():
    HOST = "0.0.0.0" 
    PORT = 8080
    
    # 將最大訊息尺寸設置為 10 MB (解決訊息過大問題)
    MAX_MESSAGE_SIZE = 10 * 1024 * 1024 
    
    logger.info(f"正在啟動 WebSocket 服務器，監聽 ws://{HOST}:{PORT}")
    
    # 啟動 WebSocket 服務器，並設定 max_size
    async with websockets.serve(
        server_handler, 
        HOST, 
        PORT,
        max_size=MAX_MESSAGE_SIZE 
    ):
        await asyncio.Future() 

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("服務器已手動關閉。")
    except Exception as e:
        logger.error(f"啟動時發生錯誤: {e}")