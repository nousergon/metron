// Minimal transactional email sender (server-only) for auth magic links.
//
// Uses Gmail SMTP via nodemailer — the same transport the alpha-engine fleet uses
// (EMAIL_SENDER + GMAIL_APP_PASSWORD app password). For the M2 public tier this swaps
// to a transactional provider (Resend/Postmark/SES) without touching callers.
//
// Fails loud: a missing credential or a send error throws, so a magic-link request
// surfaces a real error rather than silently never arriving.

import "server-only";
import nodemailer from "nodemailer";

let cached: nodemailer.Transporter | null = null;

function transport(): nodemailer.Transporter {
  if (cached) return cached;
  const user = process.env.EMAIL_SENDER;
  const pass = process.env.GMAIL_APP_PASSWORD;
  if (!user || !pass) {
    throw new Error("Email not configured — set EMAIL_SENDER and GMAIL_APP_PASSWORD to send magic links.");
  }
  cached = nodemailer.createTransport({
    host: "smtp.gmail.com",
    port: 465,
    secure: true,
    auth: { user, pass },
  });
  return cached;
}

export async function sendEmail({ to, subject, html, text }: { to: string; subject: string; html: string; text?: string }) {
  await transport().sendMail({ from: process.env.EMAIL_SENDER, to, subject, html, text });
}
