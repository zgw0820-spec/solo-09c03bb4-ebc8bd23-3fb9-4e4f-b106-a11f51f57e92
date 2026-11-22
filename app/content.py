"""发布说明页面的示例内容（两种语言、两种格式共用）。"""

RELEASES = [
    {
        "version": "2.6.0",
        "date": "2026-11-18",
        "en": {
            "title": "Release Notes",
            "summary": "Dark mode is now generally available, and the docs search is 3x faster.",
            "items": [
                "Dark mode is enabled by default for new workspaces.",
                "Docs search now uses a local index and answers in under 200 ms.",
                "Fixed a bug where cached release notes could be served in the wrong language.",
            ],
        },
        "zh": {
            "title": "发布说明",
            "summary": "深色模式正式可用，文档搜索速度提升至原来的 3 倍。",
            "items": [
                "新工作区默认启用深色模式。",
                "文档搜索改用本地索引，响应时间低于 200 毫秒。",
                "修复了缓存的发布说明可能以错误语言返回的问题。",
            ],
        },
    },
    {
        "version": "2.5.1",
        "date": "2026-10-30",
        "en": {
            "title": "Release Notes",
            "summary": "Patch release focused on reliability of the notification pipeline.",
            "items": [
                "Notifications are retried with exponential backoff.",
                "Reduced memory usage when syncing large documentation trees.",
            ],
        },
        "zh": {
            "title": "发布说明",
            "summary": "补丁版本，重点提升通知通道的可靠性。",
            "items": [
                "通知现在采用指数退避重试。",
                "降低同步大型文档树时的内存占用。",
            ],
        },
    },
]
