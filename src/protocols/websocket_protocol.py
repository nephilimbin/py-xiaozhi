import asyncio
import json
import logging
import websockets
from typing import Dict, Any, Optional

from src.constants.constants import AudioConfig
from src.protocols.protocol import Protocol, ConnectionError, MessageError
from src.utils.config_manager import ConfigManager


logger = logging.getLogger("WebSocketProtocol")


class WebSocketProtocol(Protocol):
    """WebSocket协议实现"""
    
    def __init__(self):
        """初始化WebSocket协议"""
        super().__init__()
        
        # 获取配置管理器实例
        self.config = ConfigManager.get_instance()
        
        # WebSocket连接
        self.websocket = None
        self.connected = False
        self.hello_received = None  # 初始化时先设为None
        
        # 从配置获取连接参数
        self.websocket_url = self.config.get_config("NETWORK.WEBSOCKET_URL")
        self.access_token = self.config.get_config("NETWORK.WEBSOCKET_ACCESS_TOKEN")
        self.client_id = self.config.get_client_id()
        self.device_id = self.config.get_device_id()
        
        if not self.websocket_url:
            self.logger.error("缺少WebSocket URL配置")
        if not self.access_token:
            self.logger.error("缺少WebSocket访问令牌配置")

    async def connect(self) -> bool:
        """
        连接到WebSocket服务器
        
        返回:
            bool: 连接是否成功
            
        抛出:
            ConnectionError: 当连接失败时
        """
        try:
            # 在连接时创建Event，确保在正确的事件循环中
            self.hello_received = asyncio.Event()

            # 配置连接头
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Protocol-Version": "1",
                "Device-Id": self.device_id,
                "Client-Id": self.client_id
            }

            # 建立WebSocket连接 (兼容不同Python版本的写法)
            try:
                # 新的写法 (在Python 3.11+版本中)
                self.websocket = await websockets.connect(
                    uri=self.websocket_url, 
                    additional_headers=headers
                )
            except TypeError:
                # 旧的写法 (在较早的Python版本中)
                self.websocket = await websockets.connect(
                    self.websocket_url, 
                    extra_headers=headers
                )

            # 启动消息处理循环
            asyncio.create_task(self._message_handler())

            # 发送客户端hello消息
            hello_message = {
                "type": "hello",
                "version": 1,
                "transport": "websocket",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": AudioConfig.INPUT_SAMPLE_RATE,
                    "channels": AudioConfig.CHANNELS,
                    "frame_duration": AudioConfig.FRAME_DURATION,
                }
            }
            
            # 发送Hello消息并等待服务器响应
            await self.send_text(json.dumps(hello_message))
            
            # 等待服务器Hello响应
            try:
                await asyncio.wait_for(self.hello_received.wait(), timeout=10.0)
                self.connected = True
                self.logger.info("成功连接到WebSocket服务器")
                return True
            except asyncio.TimeoutError:
                self.logger.error("等待服务器Hello响应超时")
                await self.disconnect()
                raise ConnectionError("服务器没有响应Hello消息")
                
        except Exception as e:
            self.logger.error(f"WebSocket连接失败: {e}")
            await self.disconnect()
            raise ConnectionError(f"无法连接到WebSocket服务器: {str(e)}")
            
        return False
        
    async def disconnect(self) -> bool:
        """
        断开与WebSocket服务器的连接
        
        返回:
            bool: 断开是否成功
        """
        if self.websocket:
            try:
                await self.websocket.close()
                self.logger.info("已断开WebSocket连接")
            except Exception as e:
                self.logger.error(f"断开WebSocket连接时出错: {e}")
                return False
            finally:
                self.websocket = None
                self.connected = False
                
        return True
    
    async def _message_handler(self) -> None:
        """
        WebSocket消息处理循环
        """
        if not self.websocket:
            return
            
        try:
            async for message in self.websocket:
                try:
                    if isinstance(message, str):
                        # 处理文本消息(JSON)
                        data = json.loads(message)
                        message_type = data.get("type", "")
                        
                        # 根据消息类型处理
                        if message_type == "hello":
                            await self._handle_server_hello(data)
                        elif self._on_incoming_json:
                            self._on_incoming_json(data)
                    elif isinstance(message, bytes):
                        # 处理二进制消息(音频)
                        if self._on_incoming_audio:
                            self._on_incoming_audio(message)
                except json.JSONDecodeError as e:
                    self.logger.error(f"解析JSON消息失败: {e}")
                except Exception as e:
                    self.logger.error(f"处理消息时出错: {e}")
        except websockets.ConnectionClosed:
            self.logger.info("WebSocket连接已关闭")
        except Exception as e:
            self.logger.error(f"WebSocket消息处理循环出错: {e}")
        finally:
            self.connected = False
            if self._on_network_error:
                self._on_network_error("WebSocket连接已断开")
    
    async def send_audio(self, audio_data: bytes) -> bool:
        """
        发送音频数据到服务器
        
        参数:
            audio_data: 编码后的音频数据
            
        返回:
            bool: 发送是否成功
            
        抛出:
            MessageError: 当发送失败时
        """
        if not self.websocket or not self.connected:
            return False
            
        try:
            await self.websocket.send(audio_data)
            return True
        except Exception as e:
            self._handle_error(e, "发送音频数据失败")
            raise MessageError(f"发送音频数据失败: {str(e)}")
            
        return False

    async def send_text(self, message: str) -> bool:
        """
        发送文本消息到服务器
        
        参数:
            message: 要发送的文本消息
            
        返回:
            bool: 发送是否成功
            
        抛出:
            MessageError: 当发送失败时
        """
        if not self.websocket:
            return False
            
        try:
            await self.websocket.send(message)
            return True
        except Exception as e:
            await self.close_audio_channel()
            self._handle_error(e, "发送文本消息失败")
            raise MessageError(f"发送文本消息失败: {str(e)}")
            
        return False

    def is_audio_channel_opened(self) -> bool:
        """
        检查音频通道是否已打开
        
        返回:
            bool: 音频通道是否打开
        """
        return self.websocket is not None and self.connected

    async def open_audio_channel(self) -> bool:
        """
        打开音频通道
        
        对于WebSocket，打开音频通道实际上就是连接到服务器
        
        返回:
            bool: 是否成功打开音频通道
        """
        if not self.connected:
            return await self.connect()
        return True

    async def close_audio_channel(self) -> bool:
        """
        关闭音频通道
        
        对于WebSocket，关闭音频通道实际上就是断开与服务器的连接
        
        返回:
            bool: 是否成功关闭音频通道
        """
        return await self.disconnect()

    async def _handle_server_hello(self, data: Dict[str, Any]) -> None:
        """
        处理服务器的hello消息
        
        参数:
            data: 服务器发送的hello消息数据
        """
        try:
            # 验证传输方式
            transport = data.get("transport")
            if not transport or transport != "websocket":
                self.logger.error(f"不支持的传输方式: {transport}")
                return
                
            self.logger.info("收到服务器hello消息")
            
            # 设置session_id
            self.session_id = data.get("session_id", "")

            # 设置hello接收事件
            self.hello_received.set()

            # 通知音频通道已打开
            if self._on_audio_channel_opened:
                await self._on_audio_channel_opened()

            self.logger.info("成功处理服务器hello消息")

        except Exception as e:
            self.logger.error(f"处理服务器hello消息时出错: {e}")
            if self._on_network_error:
                self._on_network_error(f"处理服务器响应失败: {str(e)}")