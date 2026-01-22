#!/usr/bin/env python3
"""
下書き管理モジュール
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional


class DraftManager:
    """下書き管理"""

    def __init__(self, drafts_path: str = "data/drafts.json"):
        self.drafts_path = drafts_path
        self.drafts = self._load()

    def _load(self) -> Dict:
        """drafts.json を読み込み"""
        if os.path.exists(self.drafts_path):
            with open(self.drafts_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {"drafts": []}

    def save(self):
        """drafts.json を保存"""
        with open(self.drafts_path, 'w', encoding='utf-8') as f:
            json.dump(self.drafts, f, indent=2, ensure_ascii=False)

    def save_draft(self, item: Dict, post_text: str) -> str:
        """下書きを保存"""
        draft_id = str(uuid.uuid4())
        draft = {
            "id": draft_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "item": item,
            "post_text": post_text,
            "status": "pending",
            "posted_at": None
        }
        self.drafts["drafts"].append(draft)
        self.save()
        return draft_id

    def get_pending_drafts(self) -> List[Dict]:
        """承認待ち下書きを取得"""
        return [d for d in self.drafts["drafts"] if d["status"] == "pending"]

    def mark_as_posted(self, draft_id: str):
        """投稿済みにマーク"""
        for draft in self.drafts["drafts"]:
            if draft["id"] == draft_id:
                draft["status"] = "posted"
                draft["posted_at"] = datetime.now(timezone.utc).isoformat()
                break
        self.save()
