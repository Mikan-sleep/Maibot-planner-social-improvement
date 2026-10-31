"""插件配置模型。

所有字段都通过 MaiBot SDK 的 ``PluginConfigBase`` 暴露到 WebUI 的插件配置页；
字段默认值就是安全默认值，``config.toml`` 只覆盖需要调整的部分。
"""

from __future__ import annotations

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    """插件生命周期配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


class SocialSection(PluginConfigBase):
    """社交策略参数。"""

    __ui_label__ = "社交策略"
    __ui_icon__ = "users"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="是否向 Planner 注入社交状态")
    bot_names: list[str] = Field(default_factory=lambda: ["柚子"], description="机器人名字/别名，用于 @ 和称呼识别")
    history_window: int = Field(default=12, description="参与判断的最近消息条数")
    recent_bot_seconds: int = Field(default=30, description="多少秒内机器人刚发过言算“刚说过”")
    burst_window_seconds: int = Field(default=12, description="统计消息速率的窗口")
    burst_threshold: int = Field(default=3, description="窗口内达到多少条算刷屏/多人快聊")
    reply_threshold: float = Field(default=0.38, description="预测分数达到多少才建议回复；越低越爱说话")
    talk_multiplier: float = Field(default=1.8, description="Maibot talk_value 的倍率；>1 提高发言频率，配合阈值一起调")
    auto_frequency: bool = Field(default=False, description="是否根据近期回复率自动微调倍率；默认关闭")
    target_reply_rate: float = Field(default=0.08, description="自动频率的目标回复率（机器人/用户，0.08=每100条约8条）")
    frequency_tune_messages: int = Field(default=30, description="每多少条用户消息触发一次自动频率调整")
    frequency_step: float = Field(default=0.1, description="自动频率每次调整的倍率步长")
    frequency_min_multiplier: float = Field(default=0.5, description="自动频率倍率下限")
    frequency_max_multiplier: float = Field(default=2.5, description="自动频率倍率上限")
    participation_mode: str = Field(default="active", description="社交参与模式：conservative / balanced / active")
    bot_tolerance: float = Field(default=0.8, description="机器人发言容忍度 0~1；越高越不把‘没点名’当负面")
    force_reply: bool = Field(default=False, description="Planner 选择 stop/wait 时，允许按策略改写为 reply；默认关闭，确认安全后再开")
    force_min_utility: float = Field(default=0.2, description="强制改写的最低策略分数；0 表示 active 模式下都可尝试")
    force_cooldown_seconds: int = Field(default=60, description="两次主动改写之间的最小间隔，避免连续刷屏")
    force_after_bot_seconds: int = Field(default=90, description="机器人刚发言多少秒内不强制改写（直接 @/引用可豁免）")
    force_lock_after_negative_seconds: int = Field(default=900, description="收到负反馈后锁定强制改写的时长")
    force_whitelist: list[str] = Field(default_factory=list, description="允许强制改写的群号；空表示不限制（仍受 force_reply 总开关约束）")
    force_auto_disable_after_negatives: int = Field(default=3, description="同一群累计多少次负反馈后自动关闭强制改写")
    learning_rate: float = Field(default=0.05, description="在线学习步长")
    max_weight: float = Field(default=6.0, description="权重绝对值上限，防止过拟合")
    neutral_limit: int = Field(default=3, description="回复后多少条中性消息视为“没接住”")
    outcome_window_seconds: int = Field(default=90, description="回复后多少秒内收集反馈")
    negative_keywords: list[str] = Field(
        default_factory=lambda: ["别插嘴", "没问你", "闭嘴", "别说了", "谁问你了", "别烦", "滚"],
        description="出现这些词时视为明确的插嘴负反馈",
    )


class PrivacySection(PluginConfigBase):
    """本地数据保留策略。"""

    __ui_label__ = "数据与隐私"
    __ui_icon__ = "shield"
    __ui_order__ = 2

    store_message_text: bool = Field(default=True, description="是否保存消息文本用于特征计算；关闭后只保存哈希/长度")
    retention_days: int = Field(default=7, description="消息和决策记录保留天数")


class LearningSection(PluginConfigBase):
    """LLM 反馈裁判与学习配置。"""

    __ui_label__ = "LLM 学习"
    __ui_icon__ = "brain"
    __ui_order__ = 3

    feedback_mode: str = Field(default="llm", description="反馈判定：llm / hybrid / keyword")
    judge_model: str = Field(default="", description="裁判模型/任务名；留空用宿主默认")
    judge_min_confidence: float = Field(default=0.55, description="低于该置信度的 LLM 判断不用于更新权重")
    judge_temperature: float = Field(default=0.0, description="裁判温度")
    judge_max_tokens: int = Field(default=180, description="裁判输出上限")
    judge_timeout_seconds: int = Field(default=20, description="LLM 裁判超时秒数；超时按中性关闭")
    cold_start_min_feedback: int = Field(default=8, description="新群至少积累多少条强反馈后才开始改权重")
    cross_group_prior: bool = Field(default=True, description="新群是否用其他群的全局平均权重做先验")
    cross_group_min_groups: int = Field(default=2, description="至少多少个有反馈的群才形成全局先验")
    reflection_enabled: bool = Field(default=True, description="是否周期性让 LLM 总结群规范")
    reflection_every: int = Field(default=12, description="每积累多少条强反馈做一次群规范反思")
    reflection_model: str = Field(default="", description="反思模型/任务名；留空用宿主默认")
    reflection_max_tokens: int = Field(default=240, description="反思输出上限")


class PlannerSocialConfig(PluginConfigBase):
    """插件总配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    social: SocialSection = Field(default_factory=SocialSection)
    privacy: PrivacySection = Field(default_factory=PrivacySection)
    learning: LearningSection = Field(default_factory=LearningSection)
