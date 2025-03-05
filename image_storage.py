import os
import sqlite3
import json
import time
from common.log import logger

class ImageStorage:
    def __init__(self, db_path, retention_days=7):
        self.db_path = db_path
        self.retention_days = retention_days
        self._init_db()
        
    def _init_db(self):
        """初始化数据库"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 创建图片信息表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY,
                    urls TEXT NOT NULL,
                    metadata TEXT,
                    create_time INTEGER NOT NULL
                )
            ''')
            
            conn.commit()
            conn.close()
            logger.info("[TYHH] Database initialized")
            
        except Exception as e:
            logger.error(f"[TYHH] Failed to initialize database: {e}")
            raise e
            
    def store_image(self, img_id: str, urls: list, metadata: dict = None):
        """存储图片信息
        Args:
            img_id: 图片ID
            urls: 图片URL列表
            metadata: 元数据字典
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 将URLs和元数据转换为JSON字符串
            urls_json = json.dumps(urls)
            metadata_json = json.dumps(metadata) if metadata else None
            
            # 插入数据
            cursor.execute(
                'INSERT OR REPLACE INTO images (id, urls, metadata, create_time) VALUES (?, ?, ?, ?)',
                (img_id, urls_json, metadata_json, int(time.time()))
            )
            
            conn.commit()
            conn.close()
            logger.debug(f"[TYHH] Stored image {img_id}")
            
        except Exception as e:
            logger.error(f"[TYHH] Failed to store image {img_id}: {e}")
            raise e
            
    def get_image(self, img_id: str) -> dict:
        """获取图片信息
        Args:
            img_id: 图片ID
        Returns:
            dict: 包含图片信息的字典，格式为：
            {
                'urls': list,  # 图片URL列表
                'metadata': dict,  # 元数据字典
                'create_time': int  # 创建时间戳
            }
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 查询数据
            cursor.execute('SELECT urls, metadata, create_time FROM images WHERE id = ?', (img_id,))
            row = cursor.fetchone()
            
            conn.close()
            
            if not row:
                return None
                
            # 解析数据
            urls = json.loads(row[0])
            metadata = json.loads(row[1]) if row[1] else None
            create_time = row[2]
            
            # 检查是否过期
            if time.time() - create_time > self.retention_days * 24 * 3600:
                self.delete_image(img_id)
                return None
                
            return {
                'urls': urls,
                'metadata': metadata,
                'create_time': create_time
            }
            
        except Exception as e:
            logger.error(f"[TYHH] Failed to get image {img_id}: {e}")
            return None
            
    def delete_image(self, img_id: str):
        """删除图片信息
        Args:
            img_id: 图片ID
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM images WHERE id = ?', (img_id,))
            
            conn.commit()
            conn.close()
            logger.debug(f"[TYHH] Deleted image {img_id}")
            
        except Exception as e:
            logger.error(f"[TYHH] Failed to delete image {img_id}: {e}")
            
    def cleanup_expired(self):
        """清理过期的图片信息"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 计算过期时间
            expire_time = int(time.time()) - self.retention_days * 24 * 3600
            
            # 删除过期数据
            cursor.execute('DELETE FROM images WHERE create_time < ?', (expire_time,))
            
            conn.commit()
            conn.close()
            logger.info("[TYHH] Cleaned up expired images")
            
        except Exception as e:
            logger.error(f"[TYHH] Failed to cleanup expired images: {e}") 