# main.py - Server with viewer connection notification
import asyncio
import json
import logging
import os
import ssl
import uuid
import time
from typing import Dict, List, Optional, Set

import av
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.cors import CORSMiddleware

from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack, RTCIceCandidate
from aiortc.contrib.media import MediaRelay

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI()

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Set up static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Store active peer connections
peer_connections: Dict[str, RTCPeerConnection] = {}
broadcaster_id: Optional[str] = None
broadcaster_websocket: Optional[WebSocket] = None
broadcaster_tracks = []  # Store broadcaster tracks
viewer_ids: Set[str] = set()  # Track connected viewers
relay = MediaRelay()

# Heartbeat interval
HEARTBEAT_INTERVAL = 30  # seconds

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Render the main page."""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/broadcast", response_class=HTMLResponse)
async def broadcast(request: Request):
    """Render the broadcaster page."""
    return templates.TemplateResponse("broadcast.html", {"request": request})

@app.get("/view", response_class=HTMLResponse)
async def view(request: Request):
    """Render the viewer page."""
    return templates.TemplateResponse("view.html", {"request": request})

async def broadcaster_heartbeat():
    """Send periodic heartbeats to the broadcaster to keep connection alive."""
    global broadcaster_websocket
    
    while True:
        if broadcaster_websocket:
            try:
                await broadcaster_websocket.send_json({
                    "type": "heartbeat",
                    "timestamp": time.time()
                })
            except Exception as e:
                logger.error(f"Error sending heartbeat: {e}")
                broadcaster_websocket = None
                
        await asyncio.sleep(HEARTBEAT_INTERVAL)

async def notify_broadcaster_viewer_connected(viewer_id: str):
    """Notify the broadcaster that a viewer has connected."""
    global broadcaster_websocket
    
    if broadcaster_websocket:
        try:
            logger.info(f"Notifying broadcaster of viewer connection: {viewer_id}")
            await broadcaster_websocket.send_json({
                "type": "viewer_connected",
                "viewer_id": viewer_id,
                "viewer_count": len(viewer_ids)
            })
        except Exception as e:
            logger.error(f"Error notifying broadcaster of viewer connection: {e}")

async def notify_broadcaster_viewer_disconnected(viewer_id: str):
    """Notify the broadcaster that a viewer has disconnected."""
    global broadcaster_websocket
    
    if broadcaster_websocket:
        try:
            logger.info(f"Notifying broadcaster of viewer disconnection: {viewer_id}")
            await broadcaster_websocket.send_json({
                "type": "viewer_disconnected",
                "viewer_id": viewer_id,
                "viewer_count": len(viewer_ids)
            })
        except Exception as e:
            logger.error(f"Error notifying broadcaster of viewer disconnection: {e}")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handle WebSocket connections for signaling."""
    global broadcaster_id, broadcaster_websocket, broadcaster_tracks, viewer_ids
    
    await websocket.accept()
    client_id = str(uuid.uuid4())
    client_role = None
    
    try:
        # Handle messages
        async for message in websocket.iter_json():
            if message["type"] == "register":
                role = message["role"]
                client_role = role
                logger.info(f"Client {client_id} registered as {role}")
                
                if role == "broadcaster":
                    broadcaster_id = client_id
                    broadcaster_websocket = websocket
                    # Clear any previous broadcaster tracks
                    broadcaster_tracks = []
                
                await websocket.send_json({"type": "registered", "id": client_id})
                    
            elif message["type"] == "offer":
                # Handle SDP offer
                logger.info(f"Received offer from client {client_id}")
                offer = RTCSessionDescription(sdp=message["sdp"], type=message["sdp_type"])
                pc = RTCPeerConnection()
                peer_connections[client_id] = pc
                
                # Set up event handlers
                @pc.on("iceconnectionstatechange")
                async def on_iceconnectionstatechange():
                    logger.info(f"ICE connection state for {client_id}: {pc.iceConnectionState}")
                    if pc.iceConnectionState == "failed":
                        await pc.close()
                        if client_id in peer_connections:
                            del peer_connections[client_id]
                
                @pc.on("track")
                async def on_track(track):
                    logger.info(f"Received track of kind {track.kind} from client {client_id}")
                    
                    if client_id == broadcaster_id and track.kind == "video":
                        logger.info("Received video track from broadcaster")
                        
                        # Store the track in our broadcaster tracks list
                        relayed_track = relay.subscribe(track)
                        broadcaster_tracks.append(relayed_track)
                        
                        # Add track to existing viewer connections
                        for viewer_id in viewer_ids:
                            if viewer_id in peer_connections:
                                logger.info(f"Adding track to existing viewer {viewer_id}")
                                peer_connections[viewer_id].addTrack(relayed_track)
                
                # Set the remote description
                await pc.setRemoteDescription(offer)
                
                # Create answer
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                
                # Send answer to client
                await websocket.send_json({
                    "type": "answer",
                    "sdp": pc.localDescription.sdp,
                    "sdp_type": pc.localDescription.type
                })
                
            elif message["type"] == "ice":
                # Handle ICE candidate
                if client_id in peer_connections:
                    try:
                        pc = peer_connections[client_id]
                        
                        # Use the candidate string directly if provided
                        if "candidate" in message:
                            await pc.addIceCandidate({
                                "candidate": message["candidate"], 
                                "sdpMid": message.get("sdpMid", ""), 
                                "sdpMLineIndex": message.get("sdpMLineIndex", 0)
                            })
                    except Exception as e:
                        logger.error(f"Error adding ICE candidate: {e}", exc_info=True)
                    
            elif message["type"] == "join":
                # Viewer wants to join the stream
                logger.info(f"Viewer {client_id} wants to join the stream")
                client_role = "viewer"
                viewer_ids.add(client_id)
                
                # Notify broadcaster of new viewer
                await notify_broadcaster_viewer_connected(client_id)
                
                if broadcaster_id and broadcaster_tracks:
                    logger.info(f"Broadcaster is active with {len(broadcaster_tracks)} tracks")
                    
                    # Create a new peer connection for the viewer
                    pc = RTCPeerConnection()
                    peer_connections[client_id] = pc
                    
                    # Add broadcaster tracks to the viewer connection
                    for track in broadcaster_tracks:
                        logger.info(f"Adding track to new viewer {client_id}")
                        pc.addTrack(track)
                    
                    # Create and send offer to viewer
                    try:
                        offer = await pc.createOffer()
                        await pc.setLocalDescription(offer)
                        
                        await websocket.send_json({
                            "type": "offer",
                            "sdp": pc.localDescription.sdp,
                            "sdp_type": pc.localDescription.type
                        })
                    except Exception as e:
                        logger.error(f"Error creating offer: {e}", exc_info=True)
                        await websocket.send_json({
                            "type": "error",
                            "message": f"Error creating offer: {str(e)}"
                        })
                else:
                    # No active broadcaster, send error to viewer
                    logger.warning(f"No active broadcaster found for viewer {client_id}")
                    await websocket.send_json({
                        "type": "error",
                        "message": "No active broadcaster found. Please try again later."
                    })
                    
            elif message["type"] == "answer":
                # Handle SDP answer from viewer
                if client_id in peer_connections:
                    pc = peer_connections[client_id]
                    answer = RTCSessionDescription(sdp=message["sdp"], type=message["sdp_type"])
                    await pc.setRemoteDescription(answer)
                    
            elif message["type"] == "heartbeat_ack":
                # Acknowledge heartbeat from broadcaster
                logger.debug(f"Received heartbeat ack from broadcaster {client_id}")
    
    except WebSocketDisconnect:
        logger.info(f"Client {client_id} disconnected")
        
        # Clean up resources
        if client_id in peer_connections:
            await peer_connections[client_id].close()
            del peer_connections[client_id]
            
        # Handle specific role disconnections
        if client_id == broadcaster_id:
            broadcaster_id = None
            broadcaster_websocket = None
            broadcaster_tracks = []
            logger.info("Broadcaster disconnected, cleared tracks")
            
            # Notify all viewers that the broadcaster has disconnected
            for viewer_id in viewer_ids:
                if viewer_id in peer_connections:
                    try:
                        viewer_ws = peer_connections[viewer_id]
                        await peer_connections[viewer_id].close()
                        del peer_connections[viewer_id]
                    except Exception:
                        pass
            
            # Clear viewer list since the broadcaster is gone
            viewer_ids.clear()
            
        elif client_role == "viewer":
            # Remove viewer from viewer set
            viewer_ids.discard(client_id)
            logger.info(f"Viewer {client_id} disconnected. Remaining viewers: {len(viewer_ids)}")
            
            # Notify broadcaster of viewer disconnection
            await notify_broadcaster_viewer_disconnected(client_id)
    
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        
        # Clean up on error
        if client_id in peer_connections:
            await peer_connections[client_id].close()
            del peer_connections[client_id]
            
        # Handle viewer disconnection on error
        if client_role == "viewer":
            viewer_ids.discard(client_id)
            await notify_broadcaster_viewer_disconnected(client_id)

@app.on_event("startup")
async def startup_event():
    """Start background tasks on app startup."""
    asyncio.create_task(broadcaster_heartbeat())

if __name__ == "__main__":
    import uvicorn
    
    # Run server
    uvicorn.run(
        "main:app", 
        host="0.0.0.0", 
        port=8000, 
        reload=True,
        ssl_keyfile="key.pem",
        ssl_certfile="cert.pem"
    )