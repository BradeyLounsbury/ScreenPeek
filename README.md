# ScreenPeek

A real-time screen sharing application using WebRTC for high-performance, low-latency streaming.

## Overview

ScreenPeek enables effortless screen sharing across networks with minimal latency. It features a clean web interface for viewers and a lightweight Python client for broadcasters, making it ideal for:

- Remote presentations
- Technical support sessions
- Collaborative work
- Educational demonstrations

## Features

- **Real-time screen sharing** with adjustable quality settings
- **Multiple viewer support** - broadcast to many people simultaneously
- **Adaptive streaming quality** that adjusts to network conditions
- **Connection statistics** for monitoring stream health
- **Secure transmission** with SSL/TLS encryption
- **Auto-reconnection** if connection is interrupted
- **Multiple monitor selection** for multi-display setups
- **Flexible resolution options** (480p to 4K)
- **Adjustable frame rate** to balance quality and performance

## Technology Stack

### Backend
- **FastAPI**: Modern, high-performance web framework
- **WebSockets**: For real-time signaling between clients and server
- **AioRTC**: Asynchronous WebRTC implementation for Python
- **Uvicorn**: ASGI server implementation

### Frontend
- **HTML/CSS/JavaScript**: Clean, responsive interface
- **WebRTC APIs**: For peer-to-peer video streaming
- **Modern browser features**: Fullscreen API, connection stats, etc.

### Screen Capture
- **MSS**: Fast, cross-platform screen capture
- **OpenCV**: Image processing and resizing
- **PyAV**: Efficient video encoding and frame management

## Installation

### Prerequisites
- Python 3.8 or higher
- pip (Python package manager)
- Modern web browser (Chrome, Firefox, Edge, etc.)

### Setup Steps

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/screenpeek.git
   cd screenpeek
   ```

2. **Create and activate a virtual environment**
   ```bash
   python -m venv venv
   
   # On Windows
   venv\Scripts\activate
   
   # On macOS/Linux
   source venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install fastapi uvicorn websockets aiortc opencv-python-headless numpy mss av
   ```

4. **Generate SSL certificates** (for development only)
   ```bash
   openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes
   ```

## Usage

### Starting the Server

Run the server using:

```bash
python main.py
```

Or with uvicorn directly:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --ssl-keyfile key.pem --ssl-certfile cert.pem
```

Access the web interface at:
```
https://localhost:8000/
```

> **Note:** You'll need to accept the self-signed certificate warning in your browser during development.

### Broadcasting Your Screen

1. Run the broadcaster client:
   ```bash
   python broadcaster.py --url wss://localhost:8000/ws --resolution 1080p --fps 30 --monitor 1
   ```

2. The script will connect to the server and wait for viewers to connect.

#### Broadcaster Options

| Option | Description | Default | Available Values |
|--------|-------------|---------|------------------|
| `--url` | WebSocket server URL | `wss://localhost:8000/ws` | Any valid WebSocket URL |
| `--resolution` | Output resolution | `1080p` | `480p`, `720p`, `1080p`, `1440p`, `4K`, `original` |
| `--fps` | Frames per second | `30` | `1-60` |
| `--monitor` | Monitor number to capture | `1` | Monitor index (depends on your setup) |

### Viewing a Stream

1. Navigate to `https://localhost:8000/` and select "View Stream"
2. Click "Connect to Stream"
3. If a broadcaster is active, the stream will begin automatically
4. Use the quality selector to adjust streaming quality based on your connection

## Project Structure

```
screenpeek/
├── main.py              # FastAPI server implementation
├── broadcaster.py       # Screen capture and broadcasting client
├── templates/           # HTML templates
│   ├── index.html       # Main landing page
│   ├── broadcast.html   # Broadcaster control page
│   └── view.html        # Viewer page with stream display
├── static/              # Static assets (if any)
├── key.pem              # SSL private key (development only)
└── cert.pem             # SSL certificate (development only)
```

## Security Considerations

- The application uses SSL/TLS for secure communication
- For production deployment:
  - Replace self-signed certificates with proper SSL certificates
  - Store certificates securely outside of version control
  - Consider implementing authentication for broadcasters
  - Possibly restrict viewer access with unique session links

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No connection to stream | Verify broadcaster is running and connected |
| Poor stream quality | Try a lower resolution or FPS setting |
| High latency | Check network conditions, reduce resolution |
| Black screen | Ensure screen capture permissions are granted |
| Connection drops | Check network stability, the broadcaster has auto-reconnect |

## Performance Optimization

- **Lower resolution**: Use 720p for general use, 480p for limited bandwidth
- **Reduce frame rate**: 15-20 FPS is sufficient for most presentations
- **Hardware acceleration**: The broadcaster attempts to use GPU acceleration when available
- **Network conditions**: Wired connections provide more stable streaming

## Contributing

Contributions are welcome! Please feel free to submit issues or pull requests.

1. Fork the repository
2. Create your feature branch: `git checkout -b feature/my-new-feature`
3. Commit your changes: `git commit -am 'Add some feature'`
4. Push to the branch: `git push origin feature/my-new-feature`
5. Submit a pull request

## License

This project is released under the MIT License. See the LICENSE file for details.

## Acknowledgments

- The WebRTC community for enabling real-time communication
- The FastAPI team for their excellent framework
- All contributors who have helped improve this project
