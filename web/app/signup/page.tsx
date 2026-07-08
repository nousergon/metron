import { redirect } from "next/navigation";
import { track } from "@/lib/track";

// Magic-link sign-in is the same flow for new and returning users (the first link
// creates the workspace), so there's no separate signup screen — send everyone to /login.
//
// This route is the funnel's signup-intent entry (metron-ops#34): a prospect landing on
// /signup fires `signup_submitted` before the redirect. Best-effort tracking — the await
// never blocks the redirect on a slow/unreachable sink. The downstream `signup_completed`
// (magic-link verified → workspace created) fires from the better-auth callback surface,
// which is gated on #32 onboarding (see NOT-in-this-PR in the PR body).
export default async function SignupPage() {
  await track("signup_submitted");
  redirect("/login");
}
