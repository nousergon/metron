import { redirect } from "next/navigation";

// Magic-link sign-in is the same flow for new and returning users (the first link
// creates the workspace), so there's no separate signup screen — send everyone to /login.
export default function SignupPage() {
  redirect("/login");
}
