"""完整形式（title_full 等）的测试项目：引擎会截断的每种文字（标题、步骤副标题 60 字，条件原文、case 模式 80 字）
各有一处超长的，另有不截断的短文字。只给 test_full_titles.py 分析，不执行。

那里把它的图去掉 *_full 字段后和 ../long_texts.graph.json 逐字节比较：那份是加 *_full 之前的引擎生成的。
"""

from fastapi import APIRouter

router = APIRouter()


class MailboxQuotaExceededError(Exception): ...


class AttachmentTooLargeForTheConfiguredLimitError(Exception): ...


class UpstreamAnalysisServiceTemporarilyUnavailableError(Exception): ...


def load(message_id):
    """读入站邮件"""


def seen_before(message_id): ...


def archive(message, reason):
    """把这封邮件连同它的全部附件一起归档到冷存储里，并在归档索引里记下原因、时间和处理它的工作进程编号，方便以后审计时按原因查找"""


def notify_the_account_owner_about_the_rejected_message(message, reason): ...


def record_the_rejection_in_the_audit_log_for_compliance(message, reason): ...


@router.get("/ping")
async def ping():
    load("ping")


@router.post("/inbound")
async def inbound(message_id):
    """处理 Gmail 推送过来的一封入站邮件：读邮件、检查附件和发件人、按内容类型分别归档，失败时通知账户所有者、记审计日志并交给重试任务"""
    message = load(message_id)
    # 先看这封邮件是不是已经处理过：同一个 message_id 可能被 Gmail 推送好几次，重复处理会让下游的统计和通知都多出一份
    seen_before(message.id)
    if message.attachments and message.sender_is_trusted and message.size_in_bytes < 10_000_000 and not message.is_spam:
        archive(message, "附件")
    for attachment in message.attachments_sorted_by_size_descending_and_then_by_file_name_for_a_stable_order:
        match attachment.content_type:
            case {"kind": "invoice", "currency": currency, "amount": amount, **rest} if currency in message.currencies:
                archive(attachment, f"{currency} {amount} {rest}")
            case "text/plain" | "text/html" | "text/markdown" | "text/csv" | "application/json" | "application/xml":
                continue
    try:
        sent = [notify_the_account_owner_about_the_rejected_message(message, "x"),
                record_the_rejection_in_the_audit_log_for_compliance(message, "x")]
    except (MailboxQuotaExceededError, AttachmentTooLargeForTheConfiguredLimitError,
            UpstreamAnalysisServiceTemporarilyUnavailableError):
        archive(message, "失败")
        raise UpstreamAnalysisServiceTemporarilyUnavailableError("上游分析服务暂时不可用，这封邮件稍后会由重试任务重新处理，不需要人工介入")
    return {"message_id": message_id, "attachments": len(message.attachments), "sent": len(sent)}
