# Spec

Before fetching an attachment, reuse the local file that was saved for it earlier, so it is not downloaded twice.

After a real fetch, keep the byte check in `save_attachment`. Do not record the attachment downloaded until that check has a real body, or until a local file was reused.

Host used in comments: `https://files.example.invalid/attachments/<id>`.
