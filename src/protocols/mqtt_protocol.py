import asyncio
import json
import logging
import time
import uuid
import socket
import threading
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
import paho.mqtt.client as mqtt
from typing import Dict, Any, Optional, Tuple

from src.utils.config_manager import ConfigManager
from src.protocols.protocol import Protocol, ConnectionError, MessageError
from src.constants.constants import AudioConfig


# 配置日志
logger = logging.getLogger("MqttProtocol")


class MqttProtocol(Protocol):
    """MQTT协议实现"""
    
    def __init__(self, loop):
        """初始化MQTT协议
        
        参数:
            loop: 事件循环
        """
        super().__init__()
        
        self.loop = loop
        self.config = ConfigManager.get_instance()
        
        # MQTT客户端
        self.mqtt_client = None
        
        # UDP相关
        self.udp_socket = None
        self.udp_thread = None
        self.udp_running = False
        
        # MQTT配置
        self.endpoint = None
        self.client_id = None
        self.username = None
        self.password = None
        self.publish_topic = None
        self.subscribe_topic = None
        
        # UDP配置
        self.udp_server = ""
        self.udp_port = 0
        self.aes_key = None
        self.aes_nonce = None
        self.local_sequence = 0
        self.remote_sequence = 0
        
        # 会话信息
        self.server_sample_rate = AudioConfig.SAMPLE_RATE
        
        # 事件
        self.server_hello_event = asyncio.Event()
        
    async def connect(self) -> bool:
        """
        连接到MQTT服务器和UDP服务器
        
        返回:
            bool: 连接是否成功
            
        抛出:
            ConnectionError: 当连接失败时
        """
        try:
            # 获取MQTT配置
            mqtt_info = self.config.get_config("MQTT_INFO")
            if not mqtt_info:
                raise ConnectionError("没有找到MQTT配置信息")
                
            # 提取配置
            self.endpoint = mqtt_info.get("endpoint")
            self.client_id = mqtt_info.get("client_id")
            self.username = mqtt_info.get("username")
            self.password = mqtt_info.get("password")
            self.publish_topic = f'from_device/{self.client_id}'
            self.subscribe_topic = f'to_device/{self.client_id}'
            
            if not all([self.endpoint, self.client_id, self.username, self.password]):
                raise ConnectionError("MQTT配置信息不完整")
            
            # 创建MQTT客户端
            self.mqtt_client = mqtt.Client(client_id=self.client_id)
            self.mqtt_client.username_pw_set(self.username, self.password)
            
            # 设置回调
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_message = self._on_mqtt_message
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            
            # 连接MQTT服务器
            host, port = self.endpoint.split(":")
            port = int(port)
            
            self.logger.info(f"正在连接MQTT服务器: {host}:{port}")
            self.mqtt_client.connect(host, port)
            
            # 在单独的线程中启动MQTT客户端循环
            self.mqtt_client.loop_start()
            
            # 发送Hello消息
            hello_message = {
                "type": "hello",
                "version": 1,
                "transport": "mqtt",
                "audio_params": {
                    "format": "opus",
                    "sample_rate": AudioConfig.INPUT_SAMPLE_RATE,
                    "channels": AudioConfig.CHANNELS,
                    "frame_duration": AudioConfig.FRAME_DURATION,
                }
            }
            
            success = await self.send_text(json.dumps(hello_message))
            if not success:
                raise ConnectionError("发送Hello消息失败")
                
            # 等待服务器Hello响应
            try:
                await asyncio.wait_for(self.server_hello_event.wait(), timeout=10.0)
                self.logger.info("成功连接到MQTT服务器")
                return True
            except asyncio.TimeoutError:
                self.logger.error("等待服务器Hello响应超时")
                await self.disconnect()
                raise ConnectionError("服务器没有响应Hello消息")
                
        except Exception as e:
            self.logger.error(f"MQTT连接失败: {e}")
            await self.disconnect()
            raise ConnectionError(f"无法连接到MQTT服务器: {str(e)}")
            
        return False
    
    async def disconnect(self) -> bool:
        """
        断开与MQTT服务器和UDP服务器的连接
        
        返回:
            bool: 断开是否成功
        """
        # 停止UDP线程
        if self.udp_running:
            self.udp_running = False
            if self.udp_thread:
                self.udp_thread.join(timeout=2.0)
                
        # 关闭UDP套接字
        if self.udp_socket:
            try:
                self.udp_socket.close()
            except Exception as e:
                self.logger.error(f"关闭UDP套接字时出错: {e}")
            finally:
                self.udp_socket = None
                
        # 断开MQTT连接
        if self.mqtt_client:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
                self.logger.info("已断开MQTT连接")
            except Exception as e:
                self.logger.error(f"断开MQTT连接时出错: {e}")
                return False
            finally:
                self.mqtt_client = None
                
        return True
    
    async def send_text(self, message: str) -> bool:
        """
        发送文本消息到MQTT服务器
        
        参数:
            message: 要发送的文本消息
            
        返回:
            bool: 发送是否成功
            
        抛出:
            MessageError: 当发送失败时
        """
        if not self.mqtt_client:
            return False
            
        try:
            result = self.mqtt_client.publish(
                self.publish_topic, 
                message, 
                qos=1
            )
            result.wait_for_publish()
            return result.is_published()
        except Exception as e:
            self._handle_error(e, "发送文本消息失败")
            raise MessageError(f"发送文本消息失败: {str(e)}")
            
        return False
    
    async def send_audio(self, audio_data: bytes) -> bool:
        """
        发送音频数据到UDP服务器
        
        参数:
            audio_data: 编码后的音频数据
            
        返回:
            bool: 发送是否成功
            
        抛出:
            MessageError: 当发送失败时
        """
        if not self.udp_socket or not self.udp_server or not self.udp_port:
            self.logger.error("UDP通道未初始化")
            return False

        try:
            # 生成新的nonce
            self.local_sequence = (self.local_sequence + 1) & 0xFFFFFFFF
            new_nonce = (
                self.aes_nonce[:4] +  # 固定前缀
                format(len(audio_data), '04x') +  # 数据长度
                self.aes_nonce[8:24] +  # 原始nonce
                format(self.local_sequence, '08x')  # 序列号
            )

            # 加密音频数据
            encrypt_encoded_data = self.aes_ctr_encrypt(
                bytes.fromhex(self.aes_key),
                bytes.fromhex(new_nonce),
                bytes(audio_data)
            )

            # 拼接nonce和密文
            packet = bytes.fromhex(new_nonce) + encrypt_encoded_data

            # 发送数据包
            self.udp_socket.sendto(packet, (self.udp_server, self.udp_port))

            # 每发送10个包打印一次日志
            if self.local_sequence % 10 == 0:
                self.logger.info(f"已发送音频数据包，序列号: {self.local_sequence}，目标: {self.udp_server}:{self.udp_port}")

            return True
        except Exception as e:
            self.logger.error(f"发送音频数据失败: {e}")
            self._handle_error(e, "发送音频数据失败")
            return False
    
    async def open_audio_channel(self) -> bool:
        """
        打开音频通道
        
        对于MQTT，打开音频通道实际上就是连接到服务器
        
        返回:
            bool: 是否成功打开音频通道
        """
        if not self.mqtt_client:
            return await self.connect()
        return True
    
    async def close_audio_channel(self) -> bool:
        """
        关闭音频通道
        
        对于MQTT，关闭音频通道实际上就是断开与服务器的连接
        
        返回:
            bool: 是否成功关闭音频通道
        """
        return await self.disconnect()
    
    def is_audio_channel_opened(self) -> bool:
        """
        检查音频通道是否已打开
        
        返回:
            bool: 音频通道是否打开
        """
        return self.udp_socket is not None and self.udp_running
    
    def _on_mqtt_connect(self, client, userdata, flags, rc):
        """MQTT连接回调"""
        if rc == 0:
            self.logger.info(f"已连接到MQTT服务器，正在订阅主题: {self.subscribe_topic}")
            client.subscribe(self.subscribe_topic, qos=1)
        else:
            self.logger.error(f"MQTT连接失败，返回码: {rc}")
            if self._on_network_error:
                asyncio.run_coroutine_threadsafe(
                    self._run_callback(self._on_network_error, f"MQTT连接失败: {rc}"),
                    self.loop
                )
    
    def _on_mqtt_message(self, client, userdata, msg):
        """MQTT消息回调"""
        try:
            payload = msg.payload.decode('utf-8')
            data = json.loads(payload)
            message_type = data.get("type", "")
            
            if message_type == "hello":
                # 处理服务器hello消息
                asyncio.run_coroutine_threadsafe(
                    self._handle_server_hello(data),
                    self.loop
                )
            elif self._on_incoming_json:
                # 处理其他JSON消息
                asyncio.run_coroutine_threadsafe(
                    self._run_callback(self._on_incoming_json, data),
                    self.loop
                )
        except json.JSONDecodeError as e:
            self.logger.error(f"解析JSON消息失败: {e}")
        except Exception as e:
            self.logger.error(f"处理MQTT消息时出错: {e}")
    
    def _on_mqtt_disconnect(self, client, userdata, rc):
        """MQTT断开连接回调"""
        self.logger.info(f"与MQTT服务器断开连接，返回码: {rc}")
        if self._on_network_error and rc != 0:
            asyncio.run_coroutine_threadsafe(
                self._run_callback(self._on_network_error, f"MQTT连接断开: {rc}"),
                self.loop
            )
    
    async def _handle_server_hello(self, data: Dict[str, Any]) -> None:
        """
        处理服务器的hello消息
        
        参数:
            data: 服务器发送的hello消息数据
        """
        try:
            # 验证传输方式
            transport = data.get("transport")
            if not transport or transport != "mqtt":
                self.logger.error(f"不支持的传输方式: {transport}")
                return
                
            self.logger.info("收到服务器hello消息")
            
            # 设置session_id
            self.session_id = data.get("session_id", "")
            
            # 获取UDP服务器信息
            udp_info = data.get("udp_audio", {})
            self.udp_server = udp_info.get("host", "")
            self.udp_port = int(udp_info.get("port", 0))
            self.aes_key = udp_info.get("aes_key", "")
            self.aes_nonce = udp_info.get("aes_nonce", "")
            
            if not all([self.udp_server, self.udp_port, self.aes_key, self.aes_nonce]):
                self.logger.error("UDP服务器信息不完整")
                return
                
            # 初始化UDP套接字
            self._init_udp_socket()
            
            # 设置hello接收事件
            self.server_hello_event.set()
            
            # 通知音频通道已打开
            if self._on_audio_channel_opened:
                self._on_audio_channel_opened()
                
            self.logger.info(f"成功处理服务器hello消息，UDP服务器: {self.udp_server}:{self.udp_port}")
            
        except Exception as e:
            self.logger.error(f"处理服务器hello消息时出错: {e}")
            if self._on_network_error:
                self._on_network_error(f"处理服务器响应失败: {str(e)}")
    
    def _init_udp_socket(self) -> None:
        """初始化UDP套接字"""
        try:
            # 关闭现有UDP套接字
            if self.udp_socket:
                self.udp_socket.close()
                
            # 创建新的UDP套接字
            self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            
            # 启动UDP接收线程
            self.udp_running = True
            self.udp_thread = threading.Thread(target=self._udp_receive_thread)
            self.udp_thread.daemon = True
            self.udp_thread.start()
            
            self.logger.info("UDP套接字初始化成功")
        except Exception as e:
            self.logger.error(f"初始化UDP套接字失败: {e}")
            if self._on_network_error:
                self._on_network_error(f"初始化UDP通道失败: {str(e)}")
    
    def _udp_receive_thread(self) -> None:
        """UDP数据接收线程"""
        self.logger.info("UDP接收线程已启动")
        
        while self.udp_running and self.udp_socket:
            try:
                # 设置接收超时
                self.udp_socket.settimeout(0.5)
                
                # 接收数据
                data, addr = self.udp_socket.recvfrom(2048)
                
                # 处理数据 (解密并转发)
                if data and len(data) > 32:  # 至少包含nonce(32字节)和一些音频数据
                    nonce = data[:32].hex()
                    encrypted_data = data[32:]
                    
                    # 解密数据
                    decrypted_data = self.aes_ctr_decrypt(
                        bytes.fromhex(self.aes_key),
                        bytes.fromhex(nonce),
                        encrypted_data
                    )
                    
                    # 转发到音频回调
                    if self._on_incoming_audio:
                        self._on_incoming_audio(decrypted_data)
                        
            except socket.timeout:
                # 超时是正常的，继续循环
                pass
            except Exception as e:
                if self.udp_running:  # 只有在非主动关闭时才记录错误
                    self.logger.error(f"UDP接收线程出错: {e}")
                    
        self.logger.info("UDP接收线程已退出")
    
    def aes_ctr_encrypt(self, key: bytes, nonce: bytes, data: bytes) -> bytes:
        """
        使用AES-CTR模式加密数据
        
        参数:
            key: 加密密钥
            nonce: 随机数
            data: 待加密数据
            
        返回:
            bytes: 加密后的数据
        """
        cipher = Cipher(
            algorithms.AES(key),
            modes.CTR(nonce),
            backend=default_backend()
        )
        encryptor = cipher.encryptor()
        return encryptor.update(data) + encryptor.finalize()
    
    def aes_ctr_decrypt(self, key: bytes, nonce: bytes, data: bytes) -> bytes:
        """
        使用AES-CTR模式解密数据
        
        参数:
            key: 解密密钥
            nonce: 随机数
            data: 待解密数据
            
        返回:
            bytes: 解密后的数据
        """
        cipher = Cipher(
            algorithms.AES(key),
            modes.CTR(nonce),
            backend=default_backend()
        )
        decryptor = cipher.decryptor()
        return decryptor.update(data) + decryptor.finalize()
        
    async def _run_callback(self, callback, *args, **kwargs):
        """
        在事件循环中运行回调函数
        
        参数:
            callback: 回调函数
            args, kwargs: 回调函数的参数
        """
        if callback:
            try:
                return callback(*args, **kwargs)
            except Exception as e:
                self.logger.error(f"执行回调函数时出错: {e}")
                return None
