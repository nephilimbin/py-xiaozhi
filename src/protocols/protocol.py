from abc import ABC, abstractmethod
from typing import Callable, Optional, Dict, Any, Union
import json
import logging
from enum import Enum

from src.constants.constants import AbortReason, ListeningMode


class ProtocolError(Exception):
    """协议错误基类"""
    pass


class ConnectionError(ProtocolError):
    """连接错误"""
    pass


class MessageError(ProtocolError):
    """消息错误"""
    pass


class Protocol(ABC):
    """
    通信协议抽象基类
    
    定义客户端与服务器通信的标准接口。所有具体协议实现(如WebSocket, MQTT)
    必须继承此类并实现所有抽象方法。
    """
    
    def __init__(self):
        """初始化协议基类"""
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session_id = ""
        
        # 初始化回调函数
        self._on_incoming_json = None
        self._on_incoming_audio = None
        self._on_audio_channel_opened = None
        self._on_audio_channel_closed = None
        self._on_network_error = None
        self._on_audio_config_changed = None
        
    def set_callbacks(self,
                     on_json: Optional[Callable[[Dict[str, Any]], None]] = None,
                     on_audio: Optional[Callable[[bytes], None]] = None,
                     on_audio_channel_opened: Optional[Callable[[], None]] = None,
                     on_audio_channel_closed: Optional[Callable[[], None]] = None,
                     on_network_error: Optional[Callable[[str], None]] = None,
                     on_audio_config_changed: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        """
        设置通信回调函数
        
        参数:
            on_json: 接收JSON消息的回调
            on_audio: 接收音频数据的回调
            on_audio_channel_opened: 音频通道打开时的回调
            on_audio_channel_closed: 音频通道关闭时的回调
            on_network_error: 网络错误的回调
            on_audio_config_changed: 音频配置变更的回调
        """
        self._on_incoming_json = on_json
        self._on_incoming_audio = on_audio
        self._on_audio_channel_opened = on_audio_channel_opened
        self._on_audio_channel_closed = on_audio_channel_closed
        self._on_network_error = on_network_error
        self._on_audio_config_changed = on_audio_config_changed
    
    @abstractmethod
    async def connect(self) -> bool:
        """
        连接到服务器
        
        返回:
            bool: 连接是否成功
        
        抛出:
            ConnectionError: 当连接失败时
        """
        pass
        
    @abstractmethod
    async def disconnect(self) -> bool:
        """
        断开与服务器的连接
        
        返回:
            bool: 断开是否成功
        """
        pass
    
    @abstractmethod
    async def send_text(self, message: str) -> bool:
        """
        发送文本消息到服务器
        
        参数:
            message: 要发送的文本消息（通常是JSON字符串）
            
        返回:
            bool: 发送是否成功
            
        抛出:
            MessageError: 当发送失败时
        """
        pass
    
    @abstractmethod
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
        pass
    
    @abstractmethod
    async def open_audio_channel(self) -> bool:
        """
        打开音频通道
        
        返回:
            bool: 是否成功打开音频通道
        """
        pass
    
    @abstractmethod
    async def close_audio_channel(self) -> bool:
        """
        关闭音频通道
        
        返回:
            bool: 是否成功关闭音频通道
        """
        pass
    
    @abstractmethod
    def is_audio_channel_opened(self) -> bool:
        """
        检查音频通道是否已打开
        
        返回:
            bool: 音频通道是否打开
        """
        pass
    
    async def send_abort_speaking(self, reason: AbortReason) -> bool:
        """
        发送中止语音的消息
        
        参数:
            reason: 中止原因，AbortReason枚举类型
            
        返回:
            bool: 发送是否成功
        """
        message = {
            "session_id": self.session_id,
            "type": "abort"
        }
        
        if reason == AbortReason.WAKE_WORD_DETECTED:
            message["reason"] = "wake_word_detected"
        
        return await self.send_text(json.dumps(message))
    
    def _handle_error(self, error: Exception, context: str = "") -> None:
        """
        统一处理错误
        
        参数:
            error: 错误对象
            context: 错误上下文描述
        """
        error_msg = f"{context}: {str(error)}" if context else str(error)
        self.logger.error(f"协议错误: {error_msg}")
        
        if self._on_network_error:
            self._on_network_error(error_msg)

    async def send_wake_word_detected(self, wake_word):
        """发送检测到唤醒词的消息"""
        message = {
            "session_id": self.session_id,
            "type": "listen",
            "state": "detect",
            "text": wake_word
        }
        await self.send_text(json.dumps(message))

    async def send_start_listening(self, mode):
        """发送开始监听的消息"""
        mode_map = {
            ListeningMode.ALWAYS_ON: "realtime",
            ListeningMode.AUTO_STOP: "auto",
            ListeningMode.MANUAL: "manual"
        }
        message = {
            "session_id": self.session_id,
            "type": "listen",
            "state": "start",
            "mode": mode_map[mode]
        }
        await self.send_text(json.dumps(message))

    async def send_stop_listening(self):
        """发送停止监听的消息"""
        message = {
            "session_id": self.session_id,
            "type": "listen",
            "state": "stop"
        }
        await self.send_text(json.dumps(message))

    async def send_iot_descriptors(self, descriptors):
        """发送物联网设备描述信息"""
        message = {
            "session_id": self.session_id,
            "type": "iot",
            "descriptors": json.loads(descriptors) if isinstance(descriptors, str) else descriptors
        }
        await self.send_text(json.dumps(message))

    async def send_iot_states(self, states):
        """发送物联网设备状态信息"""
        message = {
            "session_id": self.session_id,
            "type": "iot",
            "states": json.loads(states) if isinstance(states, str) else states
        }
        await self.send_text(json.dumps(message))