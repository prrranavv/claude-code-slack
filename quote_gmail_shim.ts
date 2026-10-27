// GMAIL_CMD for quote-present's present.sh, on the working team@ client (the gmail skill's
// client.ts: the same auth slack_ui uses). quote-present's own Gmail service account returns
// invalid_grant since the 2026-10-21 rotation; angie-miller is not modified, present.sh
// already takes GMAIL_CMD.
//
// Speaks present.sh's dialect only:
//   draft <to> <subject> --body-file <html> [--inline <png>] --as <mailbox>
//   read-draft <draftId> --as <mailbox>        (attachments as [{filename}])
//   delete-draft <draftId> --as <mailbox>
// --attach is refused: the desk never sends the carrier PDF to a customer.
import { readFileSync } from "fs";
import { basename } from "path";
import { gmail, ok, err } from "/Users/pranav/work/.agents/skills/gmail/scripts/client";
import { readDraft, deleteDraft } from "/Users/pranav/work/.agents/skills/gmail/scripts/draft";
import { encodeHeaderValue } from "/Users/pranav/work/.agents/skills/gmail/scripts/mime";

const args = process.argv.slice(2);
const opt = (name: string) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : undefined;
};

function b64lines(buf: Buffer): string {
  return buf.toString("base64").replace(/(.{76})/g, "$1\r\n");
}

function htmlToText(html: string): string {
  return html
    .replace(/<style[\s\S]*?<\/style>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|tr|h\d|div|li)>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&#39;|&rsquo;/g, "'").replace(/&quot;/g, '"')
    .replace(/[ \t]+/g, " ").replace(/\n\s*\n\s*\n+/g, "\n\n").trim();
}

export function buildRelated(to: string, subject: string, html: string, inline?: string): string {
  const alt = "alt_" + Date.now().toString(36);
  const rel = "rel_" + Date.now().toString(36);
  const altPart = [
    `Content-Type: multipart/alternative; boundary="${alt}"`, "",
    `--${alt}`, "Content-Type: text/plain; charset=UTF-8", "Content-Transfer-Encoding: base64", "",
    b64lines(Buffer.from(htmlToText(html))),
    `--${alt}`, "Content-Type: text/html; charset=UTF-8", "Content-Transfer-Encoding: base64", "",
    b64lines(Buffer.from(html)),
    `--${alt}--`,
  ];
  const head = ["MIME-Version: 1.0", `To: ${to}`, `Subject: ${encodeHeaderValue(subject)}`];
  if (!inline) return [...head, ...altPart, ""].join("\r\n");
  const name = basename(inline);
  return [
    ...head, `Content-Type: multipart/related; boundary="${rel}"`, "",
    `--${rel}`, ...altPart,
    `--${rel}`, `Content-Type: image/png; name="${name}"`, "Content-Transfer-Encoding: base64",
    `Content-ID: <${name}>`, `Content-Disposition: inline; filename="${name}"`, "",
    b64lines(readFileSync(inline)),
    `--${rel}--`, "",
  ].join("\r\n");
}

async function main() {
  const cmd = args[0];
  if (cmd === "draft") {
    const to = args[1], subject = args[2], bodyFile = opt("--body-file"), inline = opt("--inline");
    if (opt("--attach")) return err("--attach is not supported on the desk path");
    if (!to || !subject || !bodyFile) return err("usage: draft <to> <subject> --body-file <html> [--inline <png>]");
    const raw = Buffer.from(buildRelated(to, subject, readFileSync(bodyFile, "utf8"), inline)).toString("base64url");
    const res = await gmail.users.drafts.create({ userId: "me", requestBody: { message: { raw } } });
    return ok({ draftId: res.data.id, messageId: res.data.message?.id, threadId: res.data.message?.threadId });
  }
  if (cmd === "read-draft") {
    const d: any = await readDraft(args[1]);
    return ok({ ...d, attachments: (d.attachments || []).map((a: any) => (typeof a === "string" ? { filename: a } : a)) });
  }
  if (cmd === "delete-draft") return ok(await deleteDraft(args[1]));
  return err(`unsupported command for the quote shim: ${cmd}`);
}

main().catch((e) => err(String(e?.message || e)));
