import asyncio
import os
from pathlib import Path
from datetime import datetime
from loguru import logger
import yaml


class ConfigWatcher:
    """监听 settings.yaml 文件变化，支持热更新配置"""
    
    def __init__(self, settings_file: str = "config/settings.yaml", check_interval: int = 5):
        self.settings_file = Path(settings_file)
        self.check_interval = check_interval
        self.last_mtime = None
        self.callbacks = []
        self._is_running = False
        
        if self.settings_file.exists():
            self.last_mtime = self.settings_file.stat().st_mtime
            logger.info(f"ConfigWatcher 初始化: {self.settings_file}")
    
    def on_config_changed(self, callback):
        """注册配置变更回调"""
        self.callbacks.append(callback)
    
    async def start(self):
        """启动配置监听循环"""
        self._is_running = True
        logger.info("ConfigWatcher 已启动，每 {} 秒检查一次配置变更...".format(self.check_interval))
        
        while self._is_running:
            try:
                await self._check_file_change()
                await asyncio.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"ConfigWatcher 错误: {e}")
                await asyncio.sleep(self.check_interval)
    
    async def _check_file_change(self):
        """检查文件是否有变化"""
        if not self.settings_file.exists():
            return
        
        current_mtime = self.settings_file.stat().st_mtime
        if self.last_mtime is None or current_mtime <= self.last_mtime:
            return
        
        # 文件已修改，尝试加载新配置
        logger.info(f"检测到配置文件变更: {self.settings_file}")
        try:
            with open(self.settings_file, 'r', encoding='utf-8') as f:
                new_config = yaml.safe_load(f) or {}
            
            if not isinstance(new_config, dict):
                logger.error("配置文件格式错误：根必须是映射对象")
                return
            
            self.last_mtime = current_mtime
            
            # 触发所有回调
            for callback in self.callbacks:
                try:
                    await callback(new_config)
                except Exception as e:
                    logger.error(f"配置变更回调执行失败: {e}")
        
        except yaml.YAMLError as e:
            logger.error(f"YAML 解析错误: {e}")
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
    
    def stop(self):
        """停止配置监听"""
        self._is_running = False
        logger.info("ConfigWatcher 已停止")

