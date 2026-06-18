// Minimal transactional email sender (server-only) for auth magic links.
//
// Uses Resend (https://resend.com) sending over the Resend-verified `nousergon.ai`
// domain. EMAIL_SENDER (e.g. no-reply@nousergon.ai) MUST be an address on a
// Resend-verified domain so the message carries aligned SPF/DKIM/DMARC and isn't
// spam-filtered. Authenticating via a provider (rather than a personal Gmail app
// password) is what lets the From address legitimately be `@nousergon.ai`.
//
// Fails loud: a missing credential or a send error throws, so a magic-link request
// surfaces a real error rather than a link that silently never arrives.

import "server-only";
import { Resend } from "resend";

let cached: Resend | null = null;

function client(): Resend {
  if (cached) return cached;
  const key = process.env.RESEND_API_KEY;
  if (!key) {
    throw new Error("Email not configured — set RESEND_API_KEY to send magic links.");
  }
  cached = new Resend(key);
  return cached;
}

export async function sendEmail({ to, subject, html, text }: { to: string; subject: string; html: string; text?: string }) {
  const from = process.env.EMAIL_SENDER;
  if (!from) {
    throw new Error("Email not configured — set EMAIL_SENDER (a Resend-verified sender, e.g. no-reply@nousergon.ai).");
  }
  const { error } = await client().emails.send({ from, to, subject, html, text });
  if (error) {
    // Resend returns a structured error object instead of throwing — re-raise so the
    // caller (the magic-link send path) fails loud.
    throw new Error(`Failed to send email via Resend: ${error.message}`);
  }
}
