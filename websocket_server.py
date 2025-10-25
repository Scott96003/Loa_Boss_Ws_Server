import asyncio
import websockets
import json
import logging
from datetime import datetime
# 新增 FastAPI 相關導入
import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState


# 配置日誌記錄，包含時間戳和等級
# ... (日誌配置保持不變)

# ----------------------------------------------
# 1. 廣播核心變數
# ----------------------------------------------

# ... (CONNECTED_CLIENTS, MAIN_JSON_DATA, broadcast 函式保持不變)
# 注意：在 FastAPI 環境中，broadcast 函式可能需要檢查 client.state 是否為 WebSocketState.CONNECTED

# ----------------------------------------------
# 2. FastAPI 應用程式與路由
# ----------------------------------------------

# 【核心：定義 FastAPI 實例】
# Start Command 將會尋找這個名為 'app' 的實例
app = FastAPI() 

# 您的核心連線處理邏輯現在是一個 FastAPI WebSocket 路由
@app.websocket("/ws") # 這是客戶端連線的路徑，例如 wss://your-service.onrender.com/ws
async def fastapi_websocket_endpoint(websocket: WebSocket):
    # 【核心修正：新增 global CONNECTED_CLIENTS】
    global CONNECTED_CLIENTS
    # 這是您原始檔案中的 server_handler 邏輯
    global MAIN_JSON_DATA 
    
    # 1. 註冊連線
    try:
        await websocket.accept() # 接受連線
    except Exception as e:
        logger.error(f"接受連線錯誤: {e}")
        return # 無法接受連線，直接退出

    CONNECTED_CLIENTS.add(websocket)
    MAIN_JSON_DATA["users_online"] = len(CONNECTED_CLIENTS) 
    logger.info(f"新客戶端連線。當前連線數: {len(CONNECTED_CLIENTS)}")

    try:
        # 數據同步：
        sync_message = json.dumps({
            "type": "data_sync",
            "payload": MAIN_JSON_DATA 
        })
        await websocket.send_text(sync_message) # FastAPI 使用 send_text
        logger.info("已同步數據給新連線")

        # 2. 處理接收到的訊息
        while True:
            # FastAPI 使用 receive_text()
            message = await websocket.receive_text()
            
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                logger.warning(f"接收到非 JSON 訊息，忽略。")
                continue
            
            message_type = data.get("type")
            
            if message_type == "UPDATE_FROM_A":
                # ... (您的原始處理邏輯)
                update_payload = data.get("payload", {})
                
                MAIN_JSON_DATA.update({
                    "last_updated": datetime.now().isoformat(),
                    "users_online": len(CONNECTED_CLIENTS), 
                    "custom_data": update_payload 
                })
                
                logger.info(f"收到 A 的主數據更新，數據結構類型: {type(update_payload)}")
                
                # 廣播更新給所有訂閱者 B
                broadcast_message = json.dumps({
                    "type": "data_update",
                    "payload": MAIN_JSON_DATA 
                })
                
                # 注意：這裡的 broadcast 函式需要能處理 FastAPI 的 WebSocket 物件
                await broadcast(broadcast_message)
            
            elif message_type == "MESSAGE_FROM_A":
                # ... (您的原始處理邏輯)
                chat_message = data.get("content", "無內容")
                logger.info(f"收到 A 的即時訊息：{chat_message}")

                await broadcast(json.dumps({
                    "type": "chat_message",
                    "content": chat_message
                }))

            else:
                logger.info(f"收到未知訊息類型: {message_type}")

    except WebSocketDisconnect:
        # FastAPI 處理斷線的方式
        logger.info(f"客戶端關閉連線。")
    except Exception as e:
        logger.error(f"連線錯誤：{e}")
    finally:
        # 3. 移除連線
        CONNECTED_CLIENTS.discard(websocket)
        MAIN_JSON_DATA["users_online"] = len(CONNECTED_CLIENTS) 
        logger.info(f"客戶端已斷開。當前連線數: {len(CONNECTED_CLIENTS)}")

# ----------------------------------------------
# 3. 修正 broadcast 函式以適應 FastAPI WebSocket 物件
# ----------------------------------------------

# 在 FastAPI 中，廣播時需要檢查連線狀態
async def broadcast(message):
    """將訊息廣播給所有已連線的客戶端，並安全地處理斷線錯誤。"""
    
    clients_to_remove = set() 
    
    for client in CONNECTED_CLIENTS:
        # 檢查 FastAPI WebSocket 的狀態
        if client.client_state == WebSocketState.CONNECTED:
            try:
                await client.send_text(message)
            except Exception as e:
                logger.error(f"廣播時發生錯誤: {e}")
                clients_to_remove.add(client)
        else:
             clients_to_remove.add(client)


    for client in clients_to_remove:
        CONNECTED_CLIENTS.discard(client)
    
    if clients_to_remove:
        logger.info(f"已移除 {len(clients_to_remove)} 個斷線客戶端。當前連線數: {len(CONNECTED_CLIENTS)}")