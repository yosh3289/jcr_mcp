import asyncio
import sqlite3
import os
import sys
import json
from typing import Optional, Dict, List, Any
from dataclasses import dataclass
import httpx
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Context

# 配置常量
# 用相对 __file__ 的绝对路径，避免 MCP 客户端从其他 cwd 启动服务器时找不到 DB
DATABASE_PATH = str(Path(__file__).parent / "jcr.db")
DATA_UPDATE_URL = "https://raw.githubusercontent.com/hitfyd/ShowJCR/master/中科院分区表及JCR原始数据文件/"

@dataclass
class JournalInfo:
    """期刊信息数据类"""
    journal_name: str
    impact_factor: Optional[float] = None
    partition: Optional[str] = None
    category: Optional[str] = None
    warning_status: Optional[str] = None
    ccf_level: Optional[str] = None
    year: Optional[str] = None

class JCRDatabase:
    """JCR数据库管理类"""
    
    def __init__(self, db_path: str = DATABASE_PATH):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """初始化数据库"""
        if not os.path.exists(self.db_path):
            # 如果数据库不存在，创建基本表结构
            conn = sqlite3.connect(self.db_path)
            conn.close()
    
    def search_journal(self, journal_name: str, year: Optional[str] = None) -> List[JournalInfo]:
        """搜索期刊信息"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        results = []
        try:
            # 获取所有表名
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [table[0] for table in cursor.fetchall()]
            
            # 在各个表中搜索期刊
            for table in tables:
                try:
                    # 检查表结构
                    cursor.execute(f"PRAGMA table_info({table})")
                    columns = [col[1] for col in cursor.fetchall()]
                    
                    if 'Journal' not in columns:
                        continue
                    
                    # 构建查询语句
                    query = f"SELECT * FROM {table} WHERE Journal LIKE ? COLLATE NOCASE"
                    cursor.execute(query, (f"%{journal_name}%",))
                    
                    rows = cursor.fetchall()
                    column_names = [description[0] for description in cursor.description]
                    
                    for row in rows:
                        row_dict = dict(zip(column_names, row))
                        journal_info = self._parse_journal_info(row_dict, table)
                        if journal_info:
                            results.append(journal_info)
                
                except sqlite3.Error:
                    continue
        
        finally:
            conn.close()
        
        return results
    
    def _parse_journal_info(self, row_dict: Dict, table_name: str) -> Optional[JournalInfo]:
        """解析数据库行为期刊信息对象

        注意分支顺序：更具体的表名前缀（XRConferences / FQBJCR / CCFT）必须
        在通用前缀（XR / JCR / CCF）之前匹配，否则会被误归类。
        """
        def _clean(val):
            if val is None:
                return None
            s = str(val).strip()
            if s == '' or s.lower() == 'nan':
                return None
            return s

        def _is_top(val):
            """统一各表的 Top 标志位：XR 用 'Top'/空，FQBJCR 用 '是'/'否'，归一为布尔"""
            v = _clean(val)
            return v is not None and v in ('Top', '是', 'Yes', 'yes', 'True', 'true', 'TRUE')

        try:
            journal_name = row_dict.get('Journal', '')

            impact_factor = None
            partition = None
            category = None
            warning_status = None
            ccf_level = None
            year = None

            # 新锐期刊分区表 2026 的会议论文集子表（XR2026Conferences）
            if table_name.startswith('XR') and 'Conferences' in table_name:
                digits = ''.join(ch for ch in table_name if ch.isdigit())
                year = digits or None
                partition = _clean(row_dict.get('分区'))
                if partition and _is_top(row_dict.get('Top')):
                    partition = f"{partition}（Top）"
                abbrev = _clean(row_dict.get('会议缩写'))
                category = f"会议论文集（{abbrev}）" if abbrev else '会议论文集'

            # 新锐期刊分区表（XR2026 等）——含内嵌预警
            # 注意 XR 表的真实列名是「大类新锐分区」「预警标记」，与 FQBJCR 的「大类分区」不同
            elif table_name.startswith('XR'):
                year = table_name.replace('XR', '')
                partition = _clean(row_dict.get('大类新锐分区'))
                if partition and _is_top(row_dict.get('Top')):
                    partition = f"{partition}（Top）"
                category = _clean(row_dict.get('大类中文名')) or _clean(row_dict.get('大类英文名'))
                warning_status = _clean(row_dict.get('预警标记'))

            # 中科院分区表升级版（必须在 JCR 分支之前——'JCR' 是 'FQBJCR' 的子串）
            elif table_name.startswith('FQBJCR'):
                year = table_name.replace('FQBJCR', '')
                partition = _clean(row_dict.get('大类分区')) or _clean(row_dict.get('Partition'))
                if partition and _is_top(row_dict.get('Top')):
                    partition = f"{partition}（Top）"
                category = (_clean(row_dict.get('大类'))
                            or _clean(row_dict.get('领域'))
                            or _clean(row_dict.get('学科'))
                            or _clean(row_dict.get('Subject')))

            # JCR 影响因子（列名带年份后缀，如 IF(2024) / IF Quartile(2024)）
            # JCR2023 的 IF 列名实际是 ' IF(2023)'（带前导空格），故对列名做去空格归一化查找
            elif table_name.startswith('JCR'):
                year = table_name.replace('JCR', '')
                norm = {k.replace(' ', ''): v for k, v in row_dict.items() if isinstance(k, str)}
                impact_factor = (_clean(norm.get(f'IF({year})'))
                                 or _clean(row_dict.get('IF'))
                                 or _clean(row_dict.get('Impact Factor')))
                partition = (_clean(norm.get(f'IFQuartile({year})'))
                             or _clean(row_dict.get('Quartile'))
                             or _clean(row_dict.get('分区')))
                category = _clean(row_dict.get('Category')) or _clean(row_dict.get('类别'))

            # 国际期刊预警名单（列名按年份变化：预警等级（YYYY年） / 预警原因YYYY年）
            elif table_name.startswith('GJQKYJMD'):
                year = table_name.replace('GJQKYJMD', '')
                # 取第二列（Journal 之外首个非空值）作为预警信息，兼容各年列名
                for k, v in row_dict.items():
                    if k == 'Journal':
                        continue
                    cleaned = _clean(v)
                    if cleaned:
                        warning_status = cleaned
                        break

            # 计算领域高质量科技期刊分级目录（必须在 CCF 分支之前）
            elif table_name.startswith('CCFT'):
                year = table_name.replace('CCFT', '')
                t_rank = _clean(row_dict.get('T分区'))
                ccf_level = f"{t_rank}（计算领域高质量科技期刊）" if t_rank else None
                category = _clean(row_dict.get('领域')) or '计算领域高质量科技期刊'

            # CCF 推荐国际学术会议和期刊目录
            elif table_name.startswith('CCF'):
                year = table_name.replace('CCF', '')
                ccf_type = _clean(row_dict.get('CCF推荐类型')) or _clean(row_dict.get('CCF Level'))
                ccf_cat = _clean(row_dict.get('CCF推荐类别（国际学术刊物/会议）'))
                if ccf_type and ccf_cat:
                    ccf_level = f"{ccf_type}（{ccf_cat}）"
                else:
                    ccf_level = ccf_type
                category = _clean(row_dict.get('领域')) or _clean(row_dict.get('Field'))

            return JournalInfo(
                journal_name=journal_name,
                impact_factor=impact_factor,
                partition=partition,
                category=category,
                warning_status=warning_status,
                ccf_level=ccf_level,
                year=year
            )

        except Exception:
            return None

# 初始化FastMCP服务器
app = FastMCP("jcr-partition-server", port=8080)
db = JCRDatabase()

@app.tool()
async def search_journal(journal_name: str, year: Optional[str] = None) -> str:
    """
    搜索期刊信息，包括影响因子、分区、预警状态等
    
    Args:
        journal_name: 期刊名称（支持模糊搜索）
        year: 指定年份（可选，如2025、2024、2023等）
    
    Returns:
        期刊的详细信息，包括各年份的分区、影响因子等数据
    """
    try:
        results = db.search_journal(journal_name, year)
        
        if not results:
            return f"未找到期刊 '{journal_name}' 的相关信息"
        
        # 按期刊名称和年份分组整理结果
        grouped_results = {}
        for result in results:
            key = result.journal_name
            if key not in grouped_results:
                grouped_results[key] = []
            grouped_results[key].append(result)
        
        output = []
        for journal, infos in grouped_results.items():
            output.append(f"\n📚 期刊名称: {journal}")
            output.append("=" * 50)
            
            # 按年份排序
            infos.sort(key=lambda x: x.year or "0000", reverse=True)
            
            for info in infos:
                year_str = f"【{info.year}年】" if info.year else "【未知年份】"
                output.append(f"\n{year_str}")
                
                if info.impact_factor:
                    output.append(f"  📊 影响因子: {info.impact_factor}")
                
                if info.partition:
                    output.append(f"  🏆 分区: {info.partition}")
                
                if info.category:
                    output.append(f"  📖 学科类别: {info.category}")
                
                if info.warning_status:
                    output.append(f"  ⚠️ 预警状态: {info.warning_status}")
                
                if info.ccf_level:
                    output.append(f"  🏅 CCF推荐等级: {info.ccf_level}")
        
        return "\n".join(output)
    
    except Exception as e:
        return f"查询出错: {str(e)}"

@app.tool()
async def get_partition_trends(journal_name: str) -> str:
    """
    获取期刊分区变化趋势
    
    Args:
        journal_name: 期刊名称
    
    Returns:
        期刊历年分区变化趋势分析
    """
    try:
        results = db.search_journal(journal_name)
        
        if not results:
            return f"未找到期刊 '{journal_name}' 的相关信息"
        
        # 提取分区信息
        partition_data = []
        for result in results:
            if result.partition and result.year:
                partition_data.append((result.year, result.partition, result.journal_name))
        
        if not partition_data:
            return f"未找到期刊 '{journal_name}' 的分区信息"
        
        # 按年份排序
        partition_data.sort(key=lambda x: x[0])
        
        output = [f"📈 期刊分区变化趋势分析"]
        output.append("=" * 40)
        
        for year, partition, journal in partition_data:
            output.append(f"{year}年: {partition}")
        
        # 简单趋势分析
        if len(partition_data) > 1:
            output.append("\n📊 趋势分析:")
            first_partition = partition_data[0][1]
            last_partition = partition_data[-1][1]
            
            if "1区" in last_partition or "Q1" in last_partition:
                output.append("✅ 该期刊保持在顶级分区")
            elif "4区" in last_partition or "Q4" in last_partition:
                output.append("⚠️ 该期刊分区较低，发表需谨慎")
            else:
                output.append("📊 该期刊分区稳定，属于中等水平")
        
        return "\n".join(output)
    
    except Exception as e:
        return f"分析出错: {str(e)}"

@app.tool()
async def check_warning_journals(keywords: Optional[str] = None) -> str:
    """
    查询国际期刊预警名单

    覆盖两类数据源：
      1. 传统 GJQKYJMD 预警表（2020/2021/2023/2024/2025）
      2. 新锐期刊分区表 XR*（自 2026 起预警信息以"预警标记"字段内嵌，典型值 "Under Review"）

    Args:
        keywords: 关键词（可选，用于筛选特定期刊）

    Returns:
        预警期刊列表及其预警原因
    """
    try:
        conn = sqlite3.connect(db.db_path)
        cursor = conn.cursor()

        output = ["🚨 国际期刊预警名单查询结果"]
        output.append("=" * 40)

        # --- 1) 传统 GJQKYJMD 预警表 ---
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'GJQKYJMD%'")
        warning_tables = [t[0] for t in cursor.fetchall()]

        for table in sorted(warning_tables, reverse=True):
            year = table.replace('GJQKYJMD', '')
            output.append(f"\n📅 {year}年预警名单:")

            query = f"SELECT * FROM {table}"
            params = []
            if keywords:
                query += " WHERE Journal LIKE ? COLLATE NOCASE"
                params.append(f"%{keywords}%")

            cursor.execute(query, params)
            rows = cursor.fetchall()
            column_names = [description[0] for description in cursor.description]

            if rows:
                for row in rows:
                    row_dict = dict(zip(column_names, row))
                    journal_name = row_dict.get('Journal', '未知期刊')
                    # 各年份的预警列名不一致（预警等级（YYYY年） / 预警原因YYYY年），
                    # 取 Journal 之外第一个非空字段作为原因
                    reason = '未知原因'
                    for k, v in row_dict.items():
                        if k == 'Journal':
                            continue
                        if v is not None and str(v).strip() and str(v).strip().lower() != 'nan':
                            reason = str(v).strip()
                            break
                    output.append(f"  • {journal_name}: {reason}")
            else:
                if keywords:
                    output.append(f"  无匹配 '{keywords}' 的预警期刊")
                else:
                    output.append("  该年度无预警期刊数据")

        # --- 2) XR 新锐期刊分区表（非 Conferences 子表） ---
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'XR%' AND name NOT LIKE '%Conferences'"
        )
        xr_tables = [t[0] for t in cursor.fetchall()]

        for table in sorted(xr_tables, reverse=True):
            year = table.replace('XR', '')
            output.append(f"\n📅 {year}年预警期刊（新锐分区表内嵌）:")

            # XR 表中预警字段名为「预警标记」（典型值如 "Under Review"）
            query = (
                f'SELECT Journal, "预警标记" FROM {table} '
                f'WHERE "预警标记" IS NOT NULL AND TRIM("预警标记") <> \'\''
            )
            params = []
            if keywords:
                query += ' AND Journal LIKE ? COLLATE NOCASE'
                params.append(f"%{keywords}%")

            try:
                cursor.execute(query, params)
                rows = cursor.fetchall()
                if rows:
                    for journal, reason in rows:
                        output.append(f"  • {journal}: {reason}")
                else:
                    if keywords:
                        output.append(f"  无匹配 '{keywords}' 的预警期刊")
                    else:
                        output.append("  该年度无预警期刊")
            except sqlite3.Error as e:
                output.append(f"  查询失败: {e}")

        if not warning_tables and not xr_tables:
            conn.close()
            return "未找到预警期刊数据表"

        conn.close()
        return "\n".join(output)

    except Exception as e:
        return f"查询预警期刊出错: {str(e)}"

@app.tool()
async def compare_journals(journal_list: str) -> str:
    """
    比较多个期刊的综合信息
    
    Args:
        journal_list: 期刊名称列表，用逗号分隔，如"Nature,Science,Cell"
    
    Returns:
        多个期刊的对比分析结果
    """
    try:
        journals = [j.strip() for j in journal_list.split(',')]
        
        if len(journals) < 2:
            return "请至少提供2个期刊名称进行比较"
        
        output = ["📊 期刊对比分析结果"]
        output.append("=" * 50)
        
        all_results = {}
        for journal in journals:
            results = db.search_journal(journal)
            all_results[journal] = results
        
        # 生成对比表格
        output.append(f"\n{'期刊名称':<30} {'最新影响因子':<15} {'最新分区':<15} {'预警状态':<15}")
        output.append("-" * 80)
        
        for journal, results in all_results.items():
            if not results:
                output.append(f"{journal:<30} {'无数据':<15} {'无数据':<15} {'无数据':<15}")
                continue
            
            # 获取最新数据
            latest_if = "无数据"
            latest_partition = "无数据"
            warning_status = "正常"
            
            for result in results:
                if result.impact_factor:
                    latest_if = str(result.impact_factor)
                if result.partition:
                    latest_partition = result.partition
                if result.warning_status:
                    warning_status = "⚠️预警"
                    break
            
            output.append(f"{journal:<30} {latest_if:<15} {latest_partition:<15} {warning_status:<15}")
        
        # 推荐建议
        output.append("\n💡 投稿建议:")
        for journal, results in all_results.items():
            if results:
                has_warning = any(r.warning_status for r in results)
                if has_warning:
                    output.append(f"  ❌ {journal}: 该期刊在预警名单中，不建议投稿")
                else:
                    latest_partition = None
                    for result in results:
                        if result.partition:
                            latest_partition = result.partition
                            break
                    
                    if latest_partition and ("1区" in latest_partition or "Q1" in latest_partition):
                        output.append(f"  ⭐ {journal}: 顶级期刊，强烈推荐")
                    elif latest_partition and ("2区" in latest_partition or "Q2" in latest_partition):
                        output.append(f"  ✅ {journal}: 优质期刊，推荐投稿")
                    else:
                        output.append(f"  📝 {journal}: 可考虑投稿")
        
        return "\n".join(output)
    
    except Exception as e:
        return f"比较分析出错: {str(e)}"

@app.resource("jcr://database-info")
async def get_database_info() -> str:
    """获取数据库基本信息"""
    try:
        conn = sqlite3.connect(db.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [table[0] for table in cursor.fetchall()]
        
        info = ["📊 JCR分区表数据库信息"]
        info.append("=" * 30)
        info.append(f"数据库路径: {db.db_path}")
        info.append(f"数据表数量: {len(tables)}")
        info.append("\n📋 可用数据表:")
        
        for table in sorted(tables):
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            count = cursor.fetchone()[0]
            info.append(f"  • {table}: {count} 条记录")
        
        conn.close()
        return "\n".join(info)
    
    except Exception as e:
        return f"获取数据库信息出错: {str(e)}"

@app.prompt()
async def journal_analysis_prompt(journal_name: str) -> str:
    """期刊分析专用提示词模板"""
    return f"""
你是一个专业的学术期刊分析专家。请基于提供的期刊数据，对期刊 {journal_name} 进行全面分析，包括：

1. 期刊基本信息分析
2. 影响因子变化趋势
3. 分区变化情况
4. 预警状态评估
5. 投稿建议

请用专业、客观的语言进行分析，并给出具体的投稿建议。
"""

if __name__ == "__main__":
    # MCP stdio 模式下，stdout 必须保留给 JSON-RPC 通信。
    # 任何启动日志都必须写到 stderr，否则会污染协议流导致客户端断开。
    # Windows 默认 stderr 编码是 GBK/cp936，打 emoji 会抛 UnicodeEncodeError，
    # 所以这里强制切到 UTF-8。
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

    print("🚀 启动JCR分区表MCP服务器...", file=sys.stderr)
    print(f"📊 数据库路径: {DATABASE_PATH}", file=sys.stderr)
    print("🔧 可用工具:", file=sys.stderr)
    print("  • search_journal - 搜索期刊信息", file=sys.stderr)
    print("  • get_partition_trends - 获取分区趋势", file=sys.stderr)
    print("  • check_warning_journals - 查询预警期刊", file=sys.stderr)
    print("  • compare_journals - 对比期刊", file=sys.stderr)
    print("💡 提示词模板: journal_analysis_prompt", file=sys.stderr)
    print("📋 资源: jcr://database-info", file=sys.stderr)
    print("\n⚡ 服务器启动中...", file=sys.stderr)

    app.run(transport="stdio") 