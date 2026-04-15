#!/usr/bin/env python3
"""
JCR分区表数据同步脚本
从ShowJCR仓库获取最新的分区表数据并更新本地数据库
"""

import asyncio
import httpx
import sqlite3
import os
import pandas as pd
from pathlib import Path
import logging
from typing import Dict, List, Optional
from datetime import datetime

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('data_sync.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class DataSyncer:
    """数据同步器类"""
    
    def __init__(self, db_path: str = "jcr.db"):
        self.db_path = db_path
        self.base_url = "https://raw.githubusercontent.com/hitfyd/ShowJCR/master/"
        self.data_folder = "中科院分区表及JCR原始数据文件"
        
        # 数据源配置（上游文件命名已统一为 <表名>-UTF8.csv，GJQKYJMD 保留 .csv）
        self.data_sources = {
            # JCR 期刊影响因子
            "JCR2024": "JCR2024-UTF8.csv",
            "JCR2023": "JCR2023-UTF8.csv",
            "JCR2022": "JCR2022-UTF8.csv",

            # 中科院分区表升级版
            "FQBJCR2025": "FQBJCR2025-UTF8.csv",
            "FQBJCR2023": "FQBJCR2023-UTF8.csv",
            "FQBJCR2022": "FQBJCR2022-UTF8.csv",

            # 国际期刊预警名单（自 2026 起上游不再单独发布，预警信息内嵌于 XR2026.预警原因）
            "GJQKYJMD2025": "GJQKYJMD2025.csv",
            "GJQKYJMD2024": "GJQKYJMD2024.csv",
            "GJQKYJMD2023": "GJQKYJMD2023.csv",
            "GJQKYJMD2021": "GJQKYJMD2021.csv",
            "GJQKYJMD2020": "GJQKYJMD2020.csv",

            # 新锐期刊分区表 2026（xr-scholar.com 源，含内嵌预警）
            "XR2026": "XR2026-UTF8.csv",
            "XR2026Conferences": "XR2026Conferences-UTF8.csv",

            # CCF 推荐国际学术会议和期刊目录
            "CCF2026": "CCF2026-UTF8.csv",
            "CCF2022": "CCF2022-UTF8.csv",

            # 计算领域高质量科技期刊分级目录
            "CCFT2025": "CCFT2025-UTF8.csv",
            "CCFT2022": "CCFT2022-UTF8.csv",
        }
    
    async def download_file(self, url: str, local_path: str) -> bool:
        """下载文件

        注意：trust_env=False 跳过 httpx 对系统代理的自动探测——
        某些 Windows/代理环境下 CONNECT 隧道 SSL 握手会失败（报空的 ConnectError）。
        如果你所在网络必须走代理才能访问 GitHub，请显式传 proxies 参数或改用其他下载方式。
        """
        try:
            async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
                logger.info(f"正在下载: {url}")
                response = await client.get(url)
                response.raise_for_status()
                
                # 确保目录存在
                Path(local_path).parent.mkdir(parents=True, exist_ok=True)
                
                # 保存文件
                with open(local_path, 'wb') as f:
                    f.write(response.content)
                
                logger.info(f"文件已保存: {local_path}")
                return True
                
        except Exception as e:
            logger.error(f"下载失败 {url}: {e}")
            return False
    
    def create_database_tables(self):
        """创建数据库表结构"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 创建元数据表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_metadata (
            table_name TEXT PRIMARY KEY,
            last_updated TEXT,
            record_count INTEGER,
            file_hash TEXT
        )
        """)
        
        conn.commit()
        conn.close()
        logger.info("数据库表结构已创建")
    
    def import_csv_to_db(self, csv_path: str, table_name: str) -> bool:
        """将CSV文件导入数据库"""
        try:
            if not os.path.exists(csv_path):
                logger.warning(f"CSV文件不存在: {csv_path}")
                return False
            
            # 读取CSV文件
            try:
                # utf-8-sig 优先：自动剥离 BOM，避免首列名被解析成 '\ufeffJournal'
                for encoding in ['utf-8-sig', 'utf-8', 'gbk', 'gb2312']:
                    try:
                        df = pd.read_csv(csv_path, encoding=encoding)
                        logger.info(f"使用编码 {encoding} 成功读取文件")
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    logger.error(f"无法读取CSV文件 {csv_path}")
                    return False
                
            except Exception as e:
                logger.error(f"读取CSV文件失败 {csv_path}: {e}")
                return False
            
            if df.empty:
                logger.warning(f"CSV文件为空: {csv_path}")
                return False
            
            # 连接数据库
            conn = sqlite3.connect(self.db_path)
            
            # 删除现有表（如果存在）
            conn.execute(f"DROP TABLE IF EXISTS {table_name}")
            
            # 导入数据
            df.to_sql(table_name, conn, if_exists='replace', index=False)
            
            # 更新元数据
            current_time = datetime.now().isoformat()
            record_count = len(df)
            
            cursor = conn.cursor()
            cursor.execute("""
            INSERT OR REPLACE INTO sync_metadata 
            (table_name, last_updated, record_count, file_hash)
            VALUES (?, ?, ?, ?)
            """, (table_name, current_time, record_count, ""))
            
            conn.commit()
            conn.close()
            
            logger.info(f"成功导入 {table_name}: {record_count} 条记录")
            return True
            
        except Exception as e:
            logger.error(f"导入CSV失败 {csv_path}: {e}")
            return False
    
    async def sync_all_data(self, force_download: bool = False) -> Dict[str, bool]:
        """同步所有数据"""
        results = {}
        
        # 创建数据库表
        self.create_database_tables()
        
        # 创建临时下载目录
        download_dir = Path("temp_data")
        download_dir.mkdir(exist_ok=True)
        
        logger.info("开始同步JCR分区表数据...")
        
        for table_name, filename in self.data_sources.items():
            try:
                # 构建下载URL
                url = f"{self.base_url}{self.data_folder}/{filename}"
                local_path = download_dir / filename
                
                # 下载文件
                download_success = await self.download_file(url, str(local_path))
                
                if download_success:
                    # 导入到数据库
                    import_success = self.import_csv_to_db(str(local_path), table_name)
                    results[table_name] = import_success
                    
                    # 清理临时文件
                    if local_path.exists():
                        os.remove(local_path)
                        
                else:
                    results[table_name] = False
                    logger.error(f"数据源 {table_name} 同步失败")
                
            except Exception as e:
                logger.error(f"处理数据源 {table_name} 时出错: {e}")
                results[table_name] = False
        
        # 清理临时目录
        if download_dir.exists():
            try:
                download_dir.rmdir()
            except:
                pass
        
        return results
    
    def get_sync_status(self) -> Dict[str, any]:
        """获取同步状态"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("SELECT * FROM sync_metadata ORDER BY last_updated DESC")
            rows = cursor.fetchall()
            
            status = {
                "total_tables": len(rows),
                "tables": []
            }
            
            for row in rows:
                table_name, last_updated, record_count, file_hash = row
                status["tables"].append({
                    "name": table_name,
                    "last_updated": last_updated,
                    "record_count": record_count
                })
            
            conn.close()
            return status
            
        except Exception as e:
            logger.error(f"获取同步状态失败: {e}")
            return {"total_tables": 0, "tables": []}
    
    def validate_data_integrity(self) -> Dict[str, any]:
        """验证数据完整性"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 获取所有表
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [table[0] for table in cursor.fetchall()]
            
            validation_results = {
                "total_tables": len(tables),
                "valid_tables": 0,
                "issues": []
            }
            
            for table in tables:
                if table == 'sync_metadata':
                    continue
                
                try:
                    # 检查表结构
                    cursor.execute(f"PRAGMA table_info({table})")
                    columns = [col[1] for col in cursor.fetchall()]
                    
                    # 检查记录数
                    cursor.execute(f"SELECT COUNT(*) FROM {table}")
                    count = cursor.fetchone()[0]
                    
                    if count > 0 and 'Journal' in columns:
                        validation_results["valid_tables"] += 1
                    else:
                        validation_results["issues"].append({
                            "table": table,
                            "issue": f"表结构异常或无数据 (记录数: {count})"
                        })
                        
                except Exception as e:
                    validation_results["issues"].append({
                        "table": table,
                        "issue": f"验证失败: {e}"
                    })
            
            conn.close()
            return validation_results
            
        except Exception as e:
            logger.error(f"数据完整性验证失败: {e}")
            return {"total_tables": 0, "valid_tables": 0, "issues": [{"table": "unknown", "issue": str(e)}]}

async def main():
    """主函数"""
    print("🔄 JCR分区表数据同步工具")
    print("=" * 50)
    
    syncer = DataSyncer()
    
    while True:
        print("\n📋 可用操作:")
        print("1. 同步所有数据")
        print("2. 查看同步状态")
        print("3. 验证数据完整性")
        print("4. 退出")
        
        choice = input("\n请选择操作 (1-4): ").strip()
        
        if choice == "1":
            print("\n🚀 开始同步数据...")
            results = await syncer.sync_all_data()
            
            success_count = sum(1 for success in results.values() if success)
            total_count = len(results)
            
            print(f"\n📊 同步完成: {success_count}/{total_count} 成功")
            
            for table_name, success in results.items():
                status = "✅" if success else "❌"
                print(f"  {status} {table_name}")
        
        elif choice == "2":
            print("\n📊 同步状态:")
            status = syncer.get_sync_status()
            
            print(f"总表数: {status['total_tables']}")
            
            for table_info in status["tables"]:
                print(f"  📋 {table_info['name']}")
                print(f"      最后更新: {table_info['last_updated']}")
                print(f"      记录数: {table_info['record_count']}")
        
        elif choice == "3":
            print("\n🔍 验证数据完整性...")
            validation = syncer.validate_data_integrity()
            
            print(f"总表数: {validation['total_tables']}")
            print(f"有效表数: {validation['valid_tables']}")
            
            if validation['issues']:
                print("\n⚠️ 发现问题:")
                for issue in validation['issues']:
                    print(f"  • {issue['table']}: {issue['issue']}")
            else:
                print("✅ 数据完整性验证通过")
        
        elif choice == "4":
            print("👋 再见！")
            break
        
        else:
            print("❌ 无效选择")

if __name__ == "__main__":
    asyncio.run(main()) 