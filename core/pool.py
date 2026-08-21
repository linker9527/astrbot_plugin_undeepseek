"""
线程安全的账号池 —— 最空闲优先调度 + RAII Guard。
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)


class AccountPool:
    """线程安全的账号池，实现最空闲优先（most-idle-first）选择策略。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._accounts = []

    def load(self, accounts: list):
        """加载账号列表，保留已有 token 的账号对象（幂等 merge）。"""
        with self._lock:
            existing = {}
            for a in self._accounts:
                ident = self._identifier(a)
                if ident:
                    existing[ident] = a
            new_accounts = []
            seen = set()
            for acct in accounts:
                ident = self._identifier(acct)
                if not ident:
                    new_accounts.append({**acct, "_busy": False, "_last_released": 0.0})
                    continue
                if ident in seen:
                    logger.warning(f"[AccountPool] 检测到重复账号 ident={ident}，跳过")
                    continue
                seen.add(ident)
                if ident in existing:
                    old = existing[ident]
                    for k, v in acct.items():
                        old.setdefault(k, v)
                    new_accounts.append(old)
                else:
                    new_accounts.append({**acct, "_busy": False, "_last_released": 0.0})
            self._accounts = new_accounts
            logger.info(f"[AccountPool] 加载了 {len(self._accounts)} 个账号（保留 {len(existing)} 个已有）")

    def acquire(self, exclude_ids=None):
        """最空闲优先选择一个非忙碌账号。

        返回 (account_dict, guard) 或 (None, None)。
        guard 在 `with` / `__exit__` 时自动释放账号。
        """
        if exclude_ids is None:
            exclude_ids = []

        with self._lock:
            now = time.time()
            best = None
            for acct in self._accounts:
                if acct["_busy"]:
                    continue
                # 疑似风控账号（HIF 连续失败 >= 3 次）默认不参与分配
                # 风控跳过：连续3次HIF获取失败，24小时后才允许重试
                fail_count = acct.get("hif_fetch_fail_count", 0)
                if fail_count >= 3:
                    last_fail = acct.get("hif_last_fetch_fail_at", 0)
                    if now - last_fail < 86400:
                        continue
                    logger.info(f"[pool] 账号 {acct.get('email', '')} 风控冷却已过24小时，重置计数")
                    acct["hif_fetch_fail_count"] = 0
                acc_id = acct.get("email", "").strip() or acct.get("mobile", "").strip()
                if acc_id and acc_id in exclude_ids:
                    continue
                idle_time = now - acct.get("_last_released", 0)
                if best is None or idle_time > best[0]:
                    best = (idle_time, acct)

            if best is None:
                return None, None

            acct = best[1]
            acct["_busy"] = True
            guard = AccountGuard(acct, self)
            logger.info(f"[AccountPool] 分配账号: {self._identifier(acct)}")
            return acct, guard

    def release(self, account):
        """将账号放回池中（由 AccountGuard 或手动调用）。"""
        with self._lock:
            account["_busy"] = False
            account["_last_released"] = time.time()
            logger.info(f"[AccountPool] 释放账号: {self._identifier(account)}")

    def all_accounts(self):
        """返回所有账号的浅拷贝（用于健康检查等遍历）。"""
        with self._lock:
            return list(self._accounts)

    def available_count(self):
        """返回当前空闲账号数。"""
        with self._lock:
            return sum(1 for a in self._accounts if not a["_busy"])

    @staticmethod
    def _identifier(account):
        return account.get("email", "").strip() or account.get("mobile", "").strip()


class AccountGuard:
    """RAII 账号守卫 —— 离开作用域时自动释放账号。"""

    def __init__(self, account, pool: AccountPool):
        self.account = account
        self._pool = pool

    def __enter__(self):
        return self.account

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._pool.release(self.account)
        return False


# 全局单例
account_pool = AccountPool()
