"""LINE Messaging API push と DynamoDB 状態ストア。"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


class LineNotifier:
    URL = "https://api.line.me/v2/bot/message/push"

    def __init__(self, token: str, user_id: str):
        if not token or not user_id:
            raise ValueError("LINE_CHANNEL_TOKEN / LINE_USER_ID が未設定です")
        self.token, self.user_id = token, user_id

    def push(self, text: str) -> None:
        body = json.dumps({"to": self.user_id,
                           "messages": [{"type": "text", "text": text[:4900]}]}).encode("utf-8")
        last = None
        for attempt in range(3):
            req = urllib.request.Request(
                self.URL, data=body, method="POST",
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    if 200 <= r.status < 300:
                        return
                    last = f"HTTP {r.status}"
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                last = f"HTTP {e.code} {detail}"
                if e.code not in (429, 500, 502, 503, 504):
                    break  # 認証エラー等はリトライしても無駄
            except Exception as e:  # ネットワーク系
                last = repr(e)
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("LINE push failed: " + str(last))


class DynamoStore:
    def __init__(self, table_name: str):
        import boto3  # Lambda標準で入っている
        self.table = boto3.resource("dynamodb").Table(table_name)

    def get(self, pk: str):
        item = self.table.get_item(Key={"pk": pk}).get("Item")
        return json.loads(item["data"]) if item else None

    def put(self, pk: str, data: dict) -> None:
        self.table.put_item(Item={"pk": pk, "data": json.dumps(data, ensure_ascii=False)})


class FileStore:
    """ラズパイ等で使う、JSONファイルの状態ストア（原子的に書き換え）。"""

    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, encoding="utf-8") as f:
                self.d = json.load(f)
        except (FileNotFoundError, ValueError):
            self.d = {}

    def get(self, pk):
        v = self.d.get(pk)
        return json.loads(json.dumps(v)) if v is not None else None

    def put(self, pk, data):
        self.d[pk] = json.loads(json.dumps(data))
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, ensure_ascii=False)
        os.replace(tmp, self.path)


class MemoryStore:
    def __init__(self):
        self.d = {}

    def get(self, pk):
        v = self.d.get(pk)
        return json.loads(json.dumps(v)) if v is not None else None

    def put(self, pk, data):
        self.d[pk] = json.loads(json.dumps(data))
