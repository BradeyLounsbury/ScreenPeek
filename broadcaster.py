# broadcaster.py - Efficient screen capture with fixed resolutions
import asyncio
import json
import logging
import ssl
import mss
import numpy as np
import cv2
import websockets
import av
import argparse
import time
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from fractions import Fraction

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Predefined resolutions
RESOLUTIONS = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4K": (3840, 2160),
    "original": None  # Will use the original resolution
}

class ScreenCaptureTrack(VideoStreamTrack):
    """Video stream track that captures the screen with configurable resolution."""
    
    def __init__(self, resolution="1080p", fps=30, monitor_num=1):
        super().__init__()
        self.sct = mss.mss()
        
        # Select monitor (default to primary)
        if monitor_num >= len(self.sct.monitors):
            logger.warning(f"Monitor {monitor_num} not found. Using primary monitor instead.")
            self.monitor = self.sct.monitors[1]  # Primary monitor (0 is all monitors combined)
        else:
            self.monitor = self.sct.monitors[monitor_num]
            
        logger.info(f"Capturing from monitor: {self.monitor}")
        
        # Configuration
        self.resolution = RESOLUTIONS.get(resolution, None)
        if self.resolution:
            logger.info(f"Output resolution set to {self.resolution[0]}x{self.resolution[1]}")
        else:
            logger.info(f"Using original resolution: {self.monitor['width']}x{self.monitor['height']}")
            
        self.fps = fps
        self.frame_time = 1 / self.fps
        self.counter = 0
        self.started = False
        self.active = False  # Controlled by start_streaming/stop_streaming
        
        # Configure codec (pre-configure for hardware acceleration if possible)
        try:
            # Try to determine if we have hardware acceleration
            if hasattr(av.codec, 'Codec') and 'h264_nvenc' in av.codec.Codec.names():
                logger.info("NVIDIA hardware acceleration available")
                self.hw_accel = True
            elif hasattr(av.codec, 'Codec') and 'h264_amf' in av.codec.Codec.names():
                logger.info("AMD hardware acceleration available")
                self.hw_accel = True
            elif hasattr(av.codec, 'Codec') and 'h264_qsv' in av.codec.Codec.names():
                logger.info("Intel QSV hardware acceleration available")
                self.hw_accel = True
            else:
                self.hw_accel = False
                logger.info("No hardware acceleration detected, using software encoding")
        except Exception:
            self.hw_accel = False
            logger.info("Error checking hardware acceleration, using software encoding")
    
    def start_streaming(self):
        """Start screen capture."""
        logger.info("Starting screen capture")
        self.active = True
        self.started = False
        self.counter = 0
    
    def stop_streaming(self):
        """Stop screen capture."""
        logger.info("Stopping screen capture")
        self.active = False
    
    async def recv(self):
        # Get the next timestamp first thing - this should happen each frame
        pts, time_base = await self.next_timestamp()
        
        if not self.active:
            # If not actively streaming, return a black frame at very low FPS
            # to keep the connection alive but use minimal resources
            await asyncio.sleep(1.0)  # Sleep longer when inactive (1 second)
            
            # Create a black frame with the target resolution
            width = self.resolution[0] if self.resolution else self.monitor['width']
            height = self.resolution[1] if self.resolution else self.monitor['height']
            black_frame = np.zeros((height, width, 3), dtype=np.uint8)
            
            # Convert to VideoFrame
            video_frame = av.VideoFrame.from_ndarray(black_frame, format="bgr24")
            video_frame.pts = pts
            video_frame.time_base = time_base
            
            return video_frame
        
        # Reset timing when first becoming active
        if not self.started:
            self.start_time = asyncio.get_event_loop().time()
            self.started = True
            self.counter = 0
            logger.info("Screen capture started - beginning active stream")
            
        # Calculate the expected time for this frame
        expected_time = self.start_time + (self.counter * self.frame_time)
        
        # Wait if we're ahead of schedule
        now = asyncio.get_event_loop().time()
        if expected_time > now:
            await asyncio.sleep(expected_time - now)
        
        # Capture the screen
        try:
            img = self.sct.grab(self.monitor)
            
            # Convert to numpy array
            frame = np.array(img)
            
            # Convert BGRA to BGR for video encoding
            frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
            
            # Resize to target resolution if specified
            if self.resolution:
                frame = cv2.resize(frame, (self.resolution[0], self.resolution[1]))
            
            # Convert numpy array to VideoFrame
            video_frame = av.VideoFrame.from_ndarray(frame, format="bgr24")
            
        except Exception as e:
            # If screen capture fails, return a black frame
            logger.error(f"Screen capture error: {e}")
            width = self.resolution[0] if self.resolution else self.monitor['width']
            height = self.resolution[1] if self.resolution else self.monitor['height']
            black_frame = np.zeros((height, width, 3), dtype=np.uint8)
            video_frame = av.VideoFrame.from_ndarray(black_frame, format="bgr24")
        
        # Set timestamps
        video_frame.pts = pts
        video_frame.time_base = time_base
        
        # Increment counter for next frame timing
        self.counter += 1
        
        return video_frame

async def run_broadcaster(server_url="wss://localhost:8000/ws", resolution="1080p", fps=30, monitor=1):
    # SSL context to ignore certificate verification
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    try:
        logger.info(f"Connecting to {server_url}")
        
        async with websockets.connect(server_url, ssl=ssl_context) as websocket:
            logger.info("Connected to signaling server")
            
            # Create screen capture track with user settings (initially inactive)
            logger.info(f"Creating screen capture track (Resolution: {resolution}, FPS: {fps}, Monitor: {monitor})")
            video = ScreenCaptureTrack(resolution=resolution, fps=fps, monitor_num=monitor)
            
            # Track viewer count and active state
            viewer_count = 0
            active_broadcast = False
            
            # Register as broadcaster
            await websocket.send(json.dumps({
                "type": "register",
                "role": "broadcaster"
            }))
            
            registration = await websocket.recv()
            reg_data = json.loads(registration)
            logger.info(f"Registered as: {reg_data}")
            
            # Create peer connection
            pc = RTCPeerConnection()
            
            # Add the video track (it will initially send black frames at low rate)
            sender = pc.addTrack(video)
            
            # ICE state change handler
            @pc.on("iceconnectionstatechange")
            async def on_iceconnectionstatechange():
                logger.info(f"ICE Connection state: {pc.iceConnectionState}")
            
            # ICE candidate handler
            @pc.on("icecandidate")
            async def on_icecandidate(candidate):
                if candidate:
                    await websocket.send(json.dumps({
                        "type": "ice",
                        "candidate": candidate.candidate,
                        "sdpMid": candidate.sdpMid,
                        "sdpMLineIndex": candidate.sdpMLineIndex
                    }))
            
            # Create offer
            logger.info("Creating offer")
            offer = await pc.createOffer()
            
            # Set local description
            await pc.setLocalDescription(offer)
            logger.info("Local description set")
            
            # Send offer to server
            await websocket.send(json.dumps({
                "type": "offer",
                "sdp": pc.localDescription.sdp,
                "sdp_type": pc.localDescription.type
            }))
            logger.info("Offer sent")
            
            # Waiting for initial answer
            logger.info("Waiting for answer")
            response = await websocket.recv()
            answer_data = json.loads(response)
            
            if answer_data["type"] == "answer":
                # Set remote description
                logger.info("Processing answer")
                answer = RTCSessionDescription(
                    sdp=answer_data["sdp"], 
                    type=answer_data["sdp_type"]
                )
                await pc.setRemoteDescription(answer)
                logger.info("Remote description set")
                
                # Now wait for viewer messages
                logger.info("Waiting for viewers to connect...")
                
                # Keep the connection alive
                try:
                    while True:
                        # Wait for messages from the server
                        message = await asyncio.wait_for(websocket.recv(), timeout=5.0)
                        data = json.loads(message)
                        
                        if data["type"] == "viewer_connected":
                            viewer_count += 1
                            logger.info(f"Viewer connected. Total viewers: {viewer_count}")
                            
                            # Start streaming if not already active
                            if not active_broadcast:
                                logger.info("Starting active broadcast")
                                video.start_streaming()
                                active_broadcast = True
                                
                        elif data["type"] == "viewer_disconnected":
                            viewer_count = max(0, viewer_count - 1)
                            logger.info(f"Viewer disconnected. Total viewers: {viewer_count}")
                            
                            # Stop streaming if no more viewers
                            if viewer_count == 0 and active_broadcast:
                                logger.info("No more viewers, pausing broadcast")
                                video.stop_streaming()
                                active_broadcast = False
                                
                        elif data["type"] == "ice":
                            # Process ICE candidates from server
                            await pc.addIceCandidate({
                                "candidate": data["candidate"],
                                "sdpMid": data["sdpMid"],
                                "sdpMLineIndex": data["sdpMLineIndex"]
                            })
                            
                        elif data["type"] == "heartbeat":
                            # Respond to server heartbeat
                            await websocket.send(json.dumps({
                                "type": "heartbeat_ack"
                            }))
                            
                except asyncio.TimeoutError:
                    # No message received in timeout period, just continue
                    pass
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("WebSocket connection closed")
                except KeyboardInterrupt:
                    logger.info("Stopped by user")
                except Exception as e:
                    logger.error(f"Error in message loop: {e}", exc_info=True)
                finally:
                    # Clean up
                    if active_broadcast:
                        video.stop_streaming()
                    await pc.close()
            else:
                logger.error(f"Unexpected message: {answer_data}")
                
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)

async def run_broadcaster_with_reconnection(server_url, resolution, fps, monitor):
    """Run the broadcaster with automatic reconnection if the connection is lost."""
    while True:
        try:
            logger.info("Attempting to connect to server...")
            await run_broadcaster(server_url, resolution, fps, monitor)
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed, attempting to reconnect in 5 seconds...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Error in broadcaster: {e}", exc_info=True)
            logger.info("Attempting to reconnect in 10 seconds...")
            await asyncio.sleep(10)

if __name__ == "__main__":
    # Add command line arguments for customization
    parser = argparse.ArgumentParser(description="Screen Broadcaster")
    parser.add_argument("--url", default="wss://localhost:8000/ws", help="WebSocket server URL")
    parser.add_argument("--resolution", default="1080p", 
                        choices=["480p", "720p", "1080p", "1440p", "4K", "original"],
                        help="Output resolution")
    parser.add_argument("--fps", type=int, default=30, help="Frames per second")
    parser.add_argument("--monitor", type=int, default=1, help="Monitor number (1 is primary)")
    
    args = parser.parse_args()
    
    # Validate arguments
    if args.fps < 1 or args.fps > 60:
        logger.error("FPS must be between 1 and 60")
        exit(1)
    
    print("Screen Broadcaster Starting...")
    print(f"Resolution: {args.resolution}, FPS: {args.fps}, Monitor: {args.monitor}")
    print("Waiting for viewers to connect...")
    print("Press Ctrl+C to stop")
    
    try:
        # Use the new function with reconnection logic
        asyncio.run(run_broadcaster_with_reconnection(
            server_url=args.url,
            resolution=args.resolution,
            fps=args.fps,
            monitor=args.monitor
        ))
    except KeyboardInterrupt:
        print("Broadcaster stopped by user")
    except Exception as e:
        print(f"Error: {e}")