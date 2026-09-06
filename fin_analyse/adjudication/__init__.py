"""裁决收件箱：跨功能「待 owner 裁决」统一清单 + 触达（adjudication-inbox 设计页）。

producer 只经 :func:`AdjudicationInbox.reconcile` 同步真相处一次调用；
裁决执行留在各功能自己的确认面，收件箱只登记与提醒。
"""

from fin_analyse.adjudication.inbox import (
    AdjudicationInbox,
    AdjudicationInboxError,
    AdjudicationItem,
    ItemRow,
    inbox_fingerprint,
    open_default_inbox,
)

__all__ = [
    "AdjudicationInbox",
    "AdjudicationInboxError",
    "AdjudicationItem",
    "ItemRow",
    "inbox_fingerprint",
    "open_default_inbox",
]
