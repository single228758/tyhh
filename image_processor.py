import os
import io
import time
import requests
from PIL import Image
from common.log import logger
from io import BytesIO
import math

class ImageProcessor:
    def __init__(self, temp_dir):
        self.temp_dir = temp_dir
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)

    def ensure_temp_dir(self):
        """确保临时目录存在"""
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)

    def combine_images(self, image_paths, output_path):
        """将多张图片合并为一张2x2的图片
        Args:
            image_paths: 图片路径列表或URL列表
            output_path: 输出文件路径
        Returns:
            bool: 是否成功
        """
        try:
            # 确保临时目录存在
            self.ensure_temp_dir()
            
            # 获取所有图片
            pil_images = []
            original_sizes = []
            for path in image_paths[:4]:  # 最多处理4张图片
                try:
                    if path.startswith('http'):
                        response = requests.get(path, timeout=30)
                        if response.status_code == 200:
                            img_data = BytesIO(response.content)
                            img = Image.open(img_data)
                            pil_images.append(img)
                            original_sizes.append(img.size)
                    else:
                        img = Image.open(path)
                        pil_images.append(img)
                        original_sizes.append(img.size)
                except Exception as e:
                    logger.error(f"[TYHH] Failed to load image from {path}: {e}")
                    continue

            if not pil_images:
                logger.error("[TYHH] No valid images to combine")
                return False
            
            # 计算最佳目标尺寸
            max_width = max(size[0] for size in original_sizes)
            max_height = max(size[1] for size in original_sizes)
            aspect_ratio = max_width / max_height
            
            # 根据图片数量和比例确定目标尺寸
            if aspect_ratio > 1.5:  # 宽屏图片
                target_width = 1024
                target_height = int(target_width / aspect_ratio)
            elif aspect_ratio < 0.67:  # 竖屏图片
                target_height = 1024
                target_width = int(target_height * aspect_ratio)
            else:  # 接近方形的图片
                target_width = target_height = 512
            
            # 等比例缩放图片
            resized_images = []
            for img, orig_size in zip(pil_images, original_sizes):
                # 计算缩放比例
                width, height = orig_size
                ratio = min(target_width / width, target_height / height)
                new_size = (int(width * ratio), int(height * ratio))
                
                # 缩放图片
                resized = img.resize(new_size, Image.Resampling.LANCZOS)
                
                # 创建透明背景
                padded = Image.new('RGBA', (target_width, target_height), (255, 255, 255, 0))
                
                # 将缩放后的图片居中粘贴
                x = (target_width - new_size[0]) // 2
                y = (target_height - new_size[1]) // 2
                if resized.mode == 'RGB':
                    resized = resized.convert('RGBA')
                padded.paste(resized, (x, y))
                
                resized_images.append(padded)
            
            # 计算合并图片的布局
            num_images = len(resized_images)
            if num_images == 1:
                cols, rows = 1, 1
            elif num_images == 2:
                cols, rows = 2, 1
            elif num_images <= 4:
                cols, rows = 2, 2
            else:
                cols = math.ceil(math.sqrt(num_images))
                rows = math.ceil(num_images / cols)
            
            # 创建空白画布，添加边距
            margin = 4  # 分割线宽度
            canvas_width = cols * target_width + (cols - 1) * margin
            canvas_height = rows * target_height + (rows - 1) * margin
            
            # 使用白色背景
            canvas = Image.new('RGB', (canvas_width, canvas_height), 'white')
            
            # 粘贴图片到画布
            for idx, img in enumerate(resized_images):
                x = (idx % cols) * (target_width + margin)
                y = (idx // cols) * (target_height + margin)
                # 将RGBA图片转换为RGB并粘贴到画布上
                if img.mode == 'RGBA':
                    # 创建白色背景
                    bg = Image.new('RGB', img.size, 'white')
                    bg.paste(img, mask=img.split()[3])  # 使用alpha通道作为mask
                    canvas.paste(bg, (x, y))
                else:
                    canvas.paste(img, (x, y))
            
            # 保存合并后的图片
            canvas.save(output_path, 'JPEG', quality=95)
            logger.info(f"[TYHH] Successfully saved combined image to {output_path}")
            
            return True
            
        except Exception as e:
            logger.error(f"[TYHH] Error combining images: {e}")
            return False
        finally:
            # 清理PIL图片对象
            for img in pil_images:
                try:
                    img.close()
                except:
                    pass

    def cleanup_temp_files(self):
        """清理临时文件"""
        try:
            if os.path.exists(self.temp_dir):
                for file in os.listdir(self.temp_dir):
                    file_path = os.path.join(self.temp_dir, file)
                    try:
                        if os.path.isfile(file_path):
                            os.unlink(file_path)
                    except Exception as e:
                        logger.warning(f"[TYHH] Error deleting {file_path}: {e}")
            logger.info("[TYHH] Cleaned up temporary files")
        except Exception as e:
            logger.error(f"[TYHH] Error cleaning up temporary files: {e}") 