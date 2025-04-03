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
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, RTCIceCandidate
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

        # Configure codec parameters based on hardware acceleration
        self.codec_options = {}
        if self.hw_accel:
            if 'h264_nvenc' in av.codec.Codec.names():
                self.codec_name = 'h264_nvenc'
                self.codec_options = {
                    'preset': 'p4',  # Lower latency preset
                    'zerolatency': '1',  # Minimize latency
                    'tune': 'ull',  # Ultra-low latency tuning
                    'rc': 'vbr',  # Variable bitrate mode
                    'profile': 'high',  # High profile for better quality
                    'spatial-aq': '1',  # Spatial adaptive quantization
                    'temporal-aq': '1'  # Temporal adaptive quantization
                }
            elif 'h264_amf' in av.codec.Codec.names():
                self.codec_name = 'h264_amf'
                self.codec_options = {
                    'usage': 'ultralowlatency',  # Ultra-low latency mode
                    'quality': 'speed',  # Speed over quality
                    'profile': 'high',  # High profile for better quality at same bitrate
                    'rc': 'vbr_latency'  # Variable bitrate with latency constraint
                }
            elif 'h264_qsv' in av.codec.Codec.names():
                self.codec_name = 'h264_qsv'
                self.codec_options = {
                    'preset': 'veryfast',  # Fast encoding preset
                    'profile': 'high',  # High profile
                    'target_usage': '1',  # Speed priority (1=fastest, 7=quality)
                    'look_ahead': '0',  # Disable lookahead for lower latency
                    'low_power': '1'  # Low power mode if supported
                }
            else:
                # Fallback to software with optimized settings
                self.codec_name = 'libx264'
                self.codec_options = {
                    'preset': 'ultrafast',
                    'tune': 'zerolatency',
                    'profile': 'baseline'
                }
        else:
            # Software encoding with optimized settings
            self.codec_name = 'libx264'
            self.codec_options = {
                'preset': 'ultrafast',
                'tune': 'zerolatency',
                'profile': 'baseline'
            }

        # Calculate target bitrate based on resolution and fps
        if self.resolution:
            width, height = self.resolution
        else:
            width, height = self.monitor['width'], self.monitor['height']
            
        # Bitrate calculation based on resolution, fps, and content complexity
        # Base formula: bitrate = pixels × 0.1 × fps ÷ 1000 (kbps)
        pixels = width * height
        self.target_bitrate = int(pixels * 0.1 * self.fps / 1000)

        # Cap bitrate within reasonable limits
        self.target_bitrate = max(1000, min(self.target_bitrate, 15000))  # Between 1Mbps and 15Mbps
    
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
                # Use Lanczos interpolation for high-quality downscaling
                frame = cv2.resize(frame, (self.resolution[0], self.resolution[1]), interpolation=cv2.INTER_LANCZOS4)
            
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

def modify_sdp_for_bitrate(sdp, bitrate):
    """
    Modify the SDP to include bitrate information for different browsers.
    
    Args:
        sdp (str): The original SDP
        bitrate (int): Target bitrate in kbps
        
    Returns:
        str: Modified SDP
    """
    lines = sdp.split("\r\n")
    video_section = False
    video_bandwidth_added = False
    
    for i, line in enumerate(lines):
        # Identify the video media section
        if line.startswith("m=video"):
            video_section = True
        
        # Add bandwidth info after the media or connection line in video section
        if video_section and (line.startswith("c=") or line.startswith("m=video")):
            if not video_bandwidth_added:
                # Add bandwidth info after this line
                # b=TIAS is Transport Independent Application Specific in bps
                # Also add b=AS (Application Specific) in kbps for older clients
                lines.insert(i + 1, f"b=TIAS:{bitrate * 1000}")
                lines.insert(i + 2, f"b=AS:{bitrate}")
                video_bandwidth_added = True
                break
    
    # Handle codec-specific parameters
    for i, line in enumerate(lines):
        if "a=rtpmap" in line and ("H264" in line or "VP8" in line or "VP9" in line):
            pt = line.split(" ")[0].split(":")[1]
            
            # Look for existing fmtp line for this payload type
            fmtp_index = None
            for j, l in enumerate(lines):
                if f"a=fmtp:{pt}" in l:
                    fmtp_index = j
                    break
            
            # Add or modify codec parameters
            if "H264" in line:
                params = {
                    "x-google-min-bitrate": str(bitrate // 2),
                    "x-google-max-bitrate": str(bitrate),
                    "x-google-start-bitrate": str(bitrate // 1.5),
                    "max-fr": "30",  # Max framerate
                    "max-recv-width": "1920",
                    "max-recv-height": "1080"
                }
                
                if fmtp_index is not None:
                    # Modify existing fmtp line
                    orig_params = lines[fmtp_index].split(" ", 1)
                    if len(orig_params) > 1:
                        # Parse existing parameters
                        param_part = orig_params[1]
                        param_pairs = param_part.split(";")
                        existing_params = {}
                        
                        for pair in param_pairs:
                            if "=" in pair:
                                k, v = pair.split("=", 1)
                                existing_params[k] = v
                        
                        # Merge with our new params
                        existing_params.update(params)
                        
                        # Rebuild the fmtp line
                        new_param_str = ";".join([f"{k}={v}" for k, v in existing_params.items()])
                        lines[fmtp_index] = f"a=fmtp:{pt} {new_param_str}"
                    else:
                        # No parameters yet, add new ones
                        param_str = ";".join([f"{k}={v}" for k, v in params.items()])
                        lines[fmtp_index] = f"a=fmtp:{pt} {param_str}"
                else:
                    # No fmtp line yet, add a new one
                    param_str = ";".join([f"{k}={v}" for k, v in params.items()])
                    lines.insert(i + 1, f"a=fmtp:{pt} {param_str}")
            
            elif "VP8" in line or "VP9" in line:
                # Similar handling for VP8/VP9
                if fmtp_index is not None:
                    # Add parameters to existing line
                    if "max-fr" not in lines[fmtp_index]:
                        lines[fmtp_index] += f";max-fr=30"
                    if "x-google-max-bitrate" not in lines[fmtp_index]:
                        lines[fmtp_index] += f";x-google-max-bitrate={bitrate}"
                else:
                    # Add new fmtp line
                    lines.insert(i + 1, f"a=fmtp:{pt} max-fr=30;x-google-max-bitrate={bitrate}")
    
    return "\r\n".join(lines)

async def run_broadcaster_with_reconnection(server_url, resolution, fps, monitor):
    """Run the broadcaster with automatic reconnection if the connection is lost."""
    # Create a persistent ScreenCaptureTrack that we'll reuse between connections
    video = ScreenCaptureTrack(resolution=resolution, fps=fps, monitor_num=monitor)
    
    # Keep track of whether we're in an active streaming session
    active_broadcast = False
    backoff_time = 5  # Initial backoff time in seconds
    max_backoff = 60  # Maximum backoff time
    
    while True:
        try:
            logger.info("Attempting to connect to server...")
            
            # Create SSL context for secure connections
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
            async with websockets.connect(server_url, ssl=ssl_context) as websocket:
                logger.info("Connected to signaling server")
                
                # Reset backoff time on successful connection
                backoff_time = 5
                
                # Initialize connection variables
                pc = None
                viewer_count = 0
                
                # Register as broadcaster
                await websocket.send(json.dumps({
                    "type": "register",
                    "role": "broadcaster"
                }))
                
                registration = await websocket.recv()
                reg_data = json.loads(registration)
                logger.info(f"Registered as: {reg_data}")
                
                # Create new peer connection
                pc = RTCPeerConnection()
                
                # Add the video track 
                sender = pc.addTrack(video)
                
                # ICE state change handler
                @pc.on("iceconnectionstatechange")
                async def on_iceconnectionstatechange():
                    logger.info(f"ICE Connection state: {pc.iceConnectionState}")
                    if pc.iceConnectionState == "connected":
                        logger.info("ICE Connection established")
                    elif pc.iceConnectionState in ["disconnected", "failed", "closed"]:
                        logger.warning(f"ICE Connection state changed to {pc.iceConnectionState}")
                
                # ICE candidate handler
                @pc.on("icecandidate")
                async def on_icecandidate(candidate):
                    if candidate:
                        try:
                            await websocket.send(json.dumps({
                                "type": "ice",
                                "candidate": candidate.candidate,
                                "sdpMid": candidate.sdpMid,
                                "sdpMLineIndex": candidate.sdpMLineIndex
                            }))
                        except Exception as e:
                            logger.error(f"Error sending ICE candidate: {e}")
                
                # Create offer
                logger.info("Creating offer")
                offer = await pc.createOffer()

                # Modify the SDP to include bitrate information
                offer.sdp = modify_sdp_for_bitrate(offer.sdp, video.target_bitrate)
                
                # Set local description
                await pc.setLocalDescription(offer)
                logger.info(f"Local description set with target bitrate of {video.target_bitrate} kbps")
                
                # Send offer to server
                await websocket.send(json.dumps({
                    "type": "offer",
                    "sdp": pc.localDescription.sdp,
                    "sdp_type": pc.localDescription.type
                }))
                logger.info("Offer sent")
                
                try:
                    # Message handling loop
                    while True:
                        # Wait for messages with a timeout
                        try:
                            message = await asyncio.wait_for(websocket.recv(), timeout=10.0)
                            data = json.loads(message)
                            
                            if data["type"] == "answer":
                                # Set remote description
                                logger.info("Processing answer")
                                answer = RTCSessionDescription(
                                    sdp=data["sdp"], 
                                    type=data["sdp_type"]
                                )
                                await pc.setRemoteDescription(answer)
                                logger.info("Remote description set")
                                
                            elif data["type"] == "viewer_connected":
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
                                try:
                                    # Create proper RTCIceCandidate object
                                    candidate = RTCIceCandidate(
                                        sdpMid=data.get("sdpMid", ""),
                                        sdpMLineIndex=data.get("sdpMLineIndex", 0)
                                    )
                                    # Set the candidate string as an attribute
                                    candidate.candidate = data["candidate"]
                                    await pc.addIceCandidate(candidate)
                                except Exception as e:
                                    logger.error(f"Error adding ICE candidate: {e}", exc_info=True)
                                
                            elif data["type"] == "heartbeat":
                                # Respond to server heartbeat
                                await websocket.send(json.dumps({
                                    "type": "heartbeat_ack"
                                }))
                                
                        except asyncio.TimeoutError:
                            # No message received, just send a ping to keep the connection alive
                            try:
                                # Check if connection is still alive with a ping
                                # This will raise an exception if the connection is closed
                                pong_waiter = await websocket.ping()
                                await asyncio.wait_for(pong_waiter, timeout=2.0)
                                logger.debug("Ping successful, connection is alive")
                            except Exception as e:
                                # If ping fails, the connection is likely dead
                                logger.warning(f"Ping failed, connection may be dead: {e}")
                                break
                
                except websockets.exceptions.ConnectionClosed:
                    logger.warning("WebSocket connection closed")
                finally:
                    # Clean up the peer connection (but not the video track - we'll reuse it)
                    if pc:
                        await pc.close()
                    
                    # Make sure streaming is paused if the connection is lost
                    if active_broadcast:
                        logger.info("Connection lost, pausing broadcast")
                        video.stop_streaming()
                        active_broadcast = False
            
        except (websockets.exceptions.ConnectionClosed, websockets.exceptions.WebSocketException) as e:
            logger.warning(f"WebSocket error: {e}, attempting to reconnect in {backoff_time} seconds...")
            await asyncio.sleep(backoff_time)
            # Implement exponential backoff with a maximum
            backoff_time = min(backoff_time * 1.5, max_backoff)
            
        except Exception as e:
            logger.error(f"Unexpected error: {e}", exc_info=True)
            logger.info(f"Attempting to reconnect in {backoff_time} seconds...")
            await asyncio.sleep(backoff_time)
            # Implement exponential backoff with a maximum
            backoff_time = min(backoff_time * 1.5, max_backoff)

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