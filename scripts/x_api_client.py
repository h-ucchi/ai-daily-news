#!/usr/bin/env python3
"""
X (Twitter) API v2 クライアント
"""

import os
import json
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional


class XAPIClient:
    """X (Twitter) API v2 クライアント"""

    def __init__(self, bearer_token: str, oauth_credentials: Optional[Dict] = None):
        # 読み取り用（既存）
        self.bearer_token = bearer_token
        self.base_url = "https://api.twitter.com/2"
        self.headers = {"Authorization": f"Bearer {bearer_token}"}

        # 書き込み用（新規）
        if oauth_credentials:
            from requests_oauthlib import OAuth1Session
            self.oauth = OAuth1Session(
                client_key=oauth_credentials['api_key'],
                client_secret=oauth_credentials['api_secret'],
                resource_owner_key=oauth_credentials['access_token'],
                resource_owner_secret=oauth_credentials['access_token_secret']
            )
        else:
            self.oauth = None

    def get_user_id(self, username: str) -> Optional[str]:
        """ユーザー名からユーザーIDを取得"""
        url = f"{self.base_url}/users/by/username/{username}"
        response = requests.get(url, headers=self.headers)
        if response.status_code == 200:
            data = response.json()
            return data.get("data", {}).get("id")
        # エラーログ追加
        print(f"  ⚠️  X API Error (get_user_id): @{username} - HTTP {response.status_code}")
        print(f"      Response: {response.text[:200]}")
        return None

    def get_user_tweets(self, user_id: str, since_id: Optional[str] = None, max_results: int = 10) -> tuple:
        """ユーザーのツイートを取得"""
        url = f"{self.base_url}/users/{user_id}/tweets"
        params = {
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,public_metrics,conversation_id,referenced_tweets",
            "expansions": "author_id,referenced_tweets.id",
            "user.fields": "public_metrics"
        }

        if since_id:
            # since_id がある場合は start_time を使わない
            params["since_id"] = since_id
        else:
            # 初回実行時のみ start_time を使用
            start_time = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec='seconds')
            params["start_time"] = start_time

        response = requests.get(url, headers=self.headers, params=params)
        if response.status_code == 200:
            data = response.json()
            tweets = data.get("data", [])
            users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
            return tweets, users
        # エラーログ追加
        print(f"  ⚠️  X API Error (get_user_tweets): user_id={user_id} - HTTP {response.status_code}")
        print(f"      Response: {response.text[:200]}")
        return [], {}

    def search_tweets(self, query: str, since_id: Optional[str] = None, max_results: int = 10) -> tuple:
        """キーワードでツイート検索（過去24時間分）"""
        url = f"{self.base_url}/tweets/search/recent"
        params = {
            "query": query,
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,public_metrics",
            "expansions": "author_id",
            "user.fields": "public_metrics"
        }
        # 常に過去24時間を対象とする
        # since_idは使わない（start_timeと競合して0件になる問題を回避）
        start_time = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec='seconds')
        params["start_time"] = start_time

        response = requests.get(url, headers=self.headers, params=params)
        if response.status_code == 200:
            data = response.json()
            tweets = data.get("data", [])
            users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
            return tweets, users
        # エラーログ追加
        print(f"  ⚠️  X API Error (search_tweets): query='{query}' - HTTP {response.status_code}")
        print(f"      Response: {response.text[:200]}")
        return [], {}

    def post_tweet(self, text: str) -> Dict:
        """ツイート投稿"""
        if not self.oauth:
            raise ValueError("OAuth credentials not configured")

        url = f"{self.base_url}/tweets"
        payload = {"text": text}

        response = self.oauth.post(url, json=payload)

        if response.status_code == 201:
            return response.json()
        else:
            raise Exception(
                f"Failed to post tweet: {response.status_code} {response.text}"
            )

    def get_conversation_thread(self, conversation_id: str, author_id: str, max_tweets: int = 10) -> List[Dict]:
        """スレッド全体を取得（最大10ツイート）

        Args:
            conversation_id: スレッドのID（最初のツイートID）
            author_id: 投稿者のユーザーID
            max_tweets: 取得する最大ツイート数（デフォルト: 10）

        Returns:
            時系列順にソートされたツイートのリスト
        """
        url = f"{self.base_url}/tweets/search/recent"
        params = {
            "query": f"conversation_id:{conversation_id} from:{author_id}",
            "max_results": min(max_tweets, 100),
            "tweet.fields": "created_at,public_metrics,conversation_id",
            "sort_order": "recency"
        }

        response = requests.get(url, headers=self.headers, params=params)
        if response.status_code == 200:
            data = response.json()
            tweets = data.get("data", [])
            # 時系列順にソート（古い順）
            tweets.sort(key=lambda t: t["created_at"])
            return tweets
        return []

    def get_tweet_by_id(self, tweet_id: str) -> Optional[Dict]:
        """ツイートIDから1件のツイートを取得

        Args:
            tweet_id: ツイートID

        Returns:
            ツイートデータ（Dict）またはNone
        """
        url = f"{self.base_url}/tweets/{tweet_id}"
        params = {
            "tweet.fields": "created_at,public_metrics,author_id,text",
            "expansions": "author_id",
            "user.fields": "username,name"
        }

        response = requests.get(url, headers=self.headers, params=params)

        if response.status_code == 200:
            data = response.json()
            return data
        elif response.status_code == 404:
            print(f"⚠️ ツイートが見つかりません（削除済み or 非公開）: {tweet_id}")
            return None
        elif response.status_code == 429:
            print(f"⚠️ X API制限に達しました。しばらく待ってから再試行してください")
            return None
        else:
            print(f"⚠️ X API Error: HTTP {response.status_code}")
            print(f"    Response: {response.text[:200]}")
            return None
