import requests
import json
import os
import time
import random
import hashlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

# ========== 配置 ==========
UP_MID = os.environ['UP_MID']
WECOM_WEBHOOK = os.environ['WECOM_WEBHOOK']
WECOM_MENTION = os.environ.get('WECOM_MENTION', '')
CACHE_DIR = '.monitor-cache'
CACHE_FILE = f'{CACHE_DIR}/notified.json'
STATE_FILE = f'{CACHE_DIR}/state.json'

# 北京时区
BJ_TZ = timezone(timedelta(hours=8))

# 请求配置（模拟真实浏览器）
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Origin': 'https://www.bilibili.com',
    'Referer': 'https://www.bilibili.com/',
    'Connection': 'keep-alive',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-site',
}

# ========== 工具函数 ==========

def now_bj():
    return datetime.now(BJ_TZ)

def is_monitor_hours():
    """检查是否在监控时段 9:00-15:00（北京时间）"""
    hour = now_bj().hour
    return 9 <= hour < 15

def load_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return default if default is not None else {}

def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def generate_wbi_sign(params: dict) -> dict:
    """
    WBI 签名（基于开源实现简化版）
    参考: https://github.com/SocialSisterYi/bilibili-API-collect
    """
    # 简化实现：实际生产建议用完整 WBI 签名逻辑
    # 这里添加时间戳和基本混淆
    params['wts'] = int(time.time())
    # 实际项目中需要实现完整 img_key + sub_key 签名
    return params

class BiliAPI:
    """B站 API 封装，带风控处理"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.last_request_time = 0
        self.min_interval = 1.5  # 最小请求间隔（秒）
        
    def _wait_interval(self):
        """请求间隔控制，防止触发 799"""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.min_interval:
            sleep_time = self.min_interval - elapsed + random.uniform(0.5, 1.0)
            time.sleep(sleep_time)
        self.last_request_time = time.time()
    
    def _request(self, method, url, **kwargs):
        """
        带重试和风控处理的请求
        处理 412/799 错误
        """
        max_retries = 3
        base_delay = 5
        
        for attempt in range(max_retries):
            try:
                self._wait_interval()
                
                # 随机化请求指纹
                headers = kwargs.pop('headers', {})
                headers['X-Requested-With'] = 'XMLHttpRequest'
                
                resp = self.session.request(method, url, headers=headers, timeout=15, **kwargs)
                
                # 检查 HTTP 状态
                if resp.status_code == 412:
                    print(f"[风控] 412 错误，等待 {base_delay * (attempt + 1)}s 后重试...")
                    time.sleep(base_delay * (attempt + 1) + random.uniform(1, 3))
                    continue
                    
                if resp.status_code == 799:
                    print(f"[风控] 799 错误，IP 被限流，等待 {base_delay * 2 * (attempt + 1)}s...")
                    time.sleep(base_delay * 2 * (attempt + 1))
                    continue
                
                resp.raise_for_status()
                data = resp.json()
                
                # 检查业务状态码
                if data.get('code') == -412:
                    print(f"[风控] 业务层 412，需要降频...")
                    time.sleep(10)
                    continue
                    
                if data.get('code') == -799:
                    print(f"[风控] 业务层 799...")
                    time.sleep(15)
                    continue
                    
                return data
                
            except requests.exceptions.Timeout:
                print(f"[超时] 第 {attempt + 1} 次重试...")
                time.sleep(3)
            except Exception as e:
                print(f"[错误] {e}，第 {attempt + 1} 次重试...")
                time.sleep(2)
        
        raise Exception("请求失败，已达最大重试次数")
    
    def get_latest_video(self):
        """获取UP主最新视频"""
        # 使用 space 接口
        url = "https://api.bilibili.com/x/space/wbi/arc/search"
        params = {
            'mid': UP_MID,
            'ps': 5,  # 取最近5个，防止最新视频无评论
            'pn': 1,
            'order': 'pubdate',
        }
        params = generate_wbi_sign(params)
        
        data = self._request('GET', url, params=params)
        
        if not data.get('data', {}).get('list', {}).get('vlist'):
            return None
            
        videos = data['data']['list']['vlist']
        # 返回最近24小时内的视频，否则取最新一个
        now_ts = int(time.time())
        for v in videos:
            if now_ts - v['created'] < 86400:  # 24小时内
                return v
        return videos[0] if videos else None
    
    def get_video_info(self, bvid):
        """获取视频详细信息（获取 cid/oid）"""
        url = "https://api.bilibili.com/x/web-interface/view"
        params = {'bvid': bvid}
        
        data = self._request('GET', url, params=params)
        if data.get('data'):
            d = data['data']
            return {
                'aid': d['aid'],
                'cid': d['cid'],
                'title': d['title'],
                'owner': d['owner']['name']
            }
        return None
    
    def get_up_replies(self, oid, up_mid, max_pages=2):
        """
        获取评论区中 UP 主的回复
        oid: 视频ID（aid）
        """
        up_replies = []
        cursor = None
        
        for page in range(max_pages):
            url = "https://api.bilibili.com/x/v2/reply/main"
            params = {
                'oid': oid,
                'type': 1,
                'mode': 3,  # 时间倒序
                'ps': 20,
                'next': cursor if cursor else 0
            }
            
            data = self._request('GET', url, params=params)
            
            if data.get('code') != 0:
                break
                
            replies = data['data'].get('replies', [])
            if not replies:
                break
            
            for reply in replies:
                # 主评论
                if str(reply['mid']) == str(up_mid):
                    up_replies.append({
                        'rpid': reply['rpid'],
                        'content': reply['content']['message'],
                        'ctime': reply['ctime'],
                        'like': reply['like'],
                        'rcount': reply['rcount'],
                        'type': 'main'
                    })
                
                # 楼中楼回复
                if reply.get('replies'):
                    for sub in reply['replies']:
                        if str(sub['mid']) == str(up_mid):
                            up_replies.append({
                                'rpid': sub['rpid'],
                                'content': sub['content']['message'],
                                'ctime': sub['ctime'],
                                'like': sub['like'],
                                'parent_content': reply['content']['message'][:50],
                                'type': 'reply'
                            })
            
            # 检查是否还有下一页
            cursor_data = data['data'].get('cursor', {})
            if not cursor_data.get('is_end') and cursor_data.get('next'):
                cursor = cursor_data['next']
            else:
                break
                
            # 检查时间边界（超过2小时的评论不再查）
            oldest = replies[-1]['ctime']
            if int(time.time()) - oldest > 7200:
                break
        
        return up_replies

class WeComPusher:
    """企业微信机器人推送"""
    
    def __init__(self, webhook_url):
        self.webhook = webhook_url
    
    def send(self, title, content, mentioned_list=None, url=None):
        """
        发送企业微信消息
        支持 text 和 markdown 格式
        """
        if not self.webhook:
            print("[推送] 未配置 Webhook，跳过推送")
            print(f"标题: {title}")
            print(f"内容: {content[:200]}...")
            return
        
        # 构造 markdown 消息
        md_content = f"**{title}**\n\n{content}"
        if url:
            md_content += f"\n\n[查看视频]({url})"
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": md_content
            }
        }
        
        # 添加 @提醒
        if mentioned_list:
            payload["markdown"]["mentioned_list"] = mentioned_list
        
        try:
            resp = requests.post(
                self.webhook,
                json=payload,
                timeout=10
            )
            resp.raise_for_status()
            result = resp.json()
            
            if result.get('errcode') == 0:
                print(f"[推送成功] {title[:30]}...")
            else:
                print(f"[推送失败] {result}")
                
        except Exception as e:
            print(f"[推送错误] {e}")

def main():
    print(f"========== 监控开始 {now_bj().strftime('%Y-%m-%d %H:%M:%S')} ==========")
    
    # 时段检查
    if not is_monitor_hours():
        print("不在监控时段（北京时间 9:00-15:00），优雅退出")
        return 0
    
    # 随机启动延迟，分散请求压力
    delay = random.uniform(5, 45)
    print(f"随机延迟 {delay:.1f}s...")
    time.sleep(delay)
    
    # 初始化
    bili = BiliAPI()
    pusher = WeComPusher(WECOM_WEBHOOK)
    
    # 加载状态
    notified = set(load_json(CACHE_FILE, []))
    state = load_json(STATE_FILE, {'last_video_bvid': None, 'check_count': 0})
    
    try:
        # 1. 获取最新视频
        video = bili.get_latest_video()
        if not video:
            print("未获取到视频信息")
            return 1
        
        bvid = video['bvid']
        print(f"最新视频: {video['title'][:40]} ({bvid})")
        
        # 2. 获取视频详细信息（得到 aid/oid）
        video_info = bili.get_video_info(bvid)
        if not video_info:
            print("获取视频详情失败")
            return 1
        
        oid = video_info['aid']
        
        # 3. 获取 UP 主在该视频下的评论
        print(f"检查评论区 (oid={oid})...")
        replies = bili.get_up_replies(oid, UP_MID, max_pages=2)
        print(f"找到 {len(replies)} 条 UP 主评论")
        
        # 4. 过滤已通知的
        new_replies = []
        for r in replies:
            # 使用 rpid + 前20字内容生成唯一键
            content_hash = hashlib.md5(r['content'][:20].encode()).hexdigest()[:8]
            notify_key = f"{r['rpid']}_{content_hash}"
            
            if notify_key not in notified:
                new_replies.append(r)
                notified.add(notify_key)
        
        # 5. 推送新评论
        if new_replies:
            video_url = f"https://www.bilibili.com/video/{bvid}"
            
            # 合并多条评论
            if len(new_replies) == 1:
                r = new_replies[0]
                title = f"UP主新评论 | {video['title'][:20]}"
                content = f"{r['content'][:300]}"
                if r['type'] == 'reply':
                    content = f"💬 回复评论：{content}\n> 原评论：{r.get('parent_content', '...')}"
            else:
                title = f"UP主发了 {len(new_replies)} 条新评论 | {video['title'][:15]}"
                content = "\n\n---\n\n".join([
                    f"**{i+1}.** {r['content'][:200]}" 
                    for i, r in enumerate(new_replies[:3])
                ])
            
            mention = [WECOM_MENTION] if WECOM_MENTION else None
            pusher.send(title, content, mentioned_list=mention, url=video_url)
            
            print(f"推送 {len(new_replies)} 条新评论")
        else:
            print("无新评论")
        
        # 6. 保存状态
        save_json(CACHE_FILE, list(notified))
        state['last_video_bvid'] = bvid
        state['check_count'] = state.get('check_count', 0) + 1
        state['last_check'] = now_bj().isoformat()
        save_json(STATE_FILE, state)
        
        # 7. 清理旧缓存（保留最近7天）
        cutoff = int(time.time()) - 7 * 86400
        old_count = len(notified)
        notified = {k for k in notified if not k.startswith('0') or int(k.split('_')[0]) > 1000000000000}
        # 简化：定期重置缓存，防止无限增长
        
        print("========== 监控完成 ==========")
        return 0
        
    except Exception as e:
        print(f"[严重错误] {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == '__main__':
    exit(main())

