"""
企业微信消息加解密

WeComCrypto — AES-CBC 加解密（与原项目一致）
"""
from __future__ import annotations

import base64
import hashlib
import socket
import struct
import xml.etree.ElementTree as ET
from typing import Dict, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


class WeComCrypto:
    """企业微信消息 AES-CBC 加解密"""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        self.token = token
        self.corp_id = corp_id
        self.aes_key = base64.b64decode(encoding_aes_key + "=")

    def verify_signature(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echostr_or_encrypt: str,
    ) -> bool:
        sort_list = sorted([self.token, timestamp, nonce, echostr_or_encrypt])
        sha1 = hashlib.sha1("".join(sort_list).encode("utf-8")).hexdigest()
        return sha1 == msg_signature

    def decrypt(self, encrypted: str) -> tuple:
        aes_msg = base64.b64decode(encrypted)
        iv = self.aes_key[:16]
        cipher = Cipher(algorithms.AES(self.aes_key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(aes_msg) + decryptor.finalize()
        unpadder = PKCS7(256).unpadder()
        decrypted = unpadder.update(decrypted) + unpadder.finalize()
        msg_len = socket.ntohl(struct.unpack("I", decrypted[16:20])[0])
        msg_content = decrypted[20 : 20 + msg_len].decode("utf-8")
        receive_id = decrypted[20 + msg_len :].decode("utf-8")
        return msg_content, receive_id

    def decrypt_echostr(
        self,
        msg_signature: str,
        timestamp: str,
        nonce: str,
        echostr: str,
    ) -> Optional[str]:
        if not self.verify_signature(msg_signature, timestamp, nonce, echostr):
            return None
        content, _ = self.decrypt(echostr)
        return content


def parse_wecom_xml(xml_str: str) -> Dict[str, str]:
    """解析企微回调 XML 消息体"""
    result = {}
    try:
        root = ET.fromstring(xml_str)
        for child in root:
            result[child.tag] = child.text or ""
    except ET.ParseError:
        pass
    return result
