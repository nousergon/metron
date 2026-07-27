import Link from "next/link";
import { MagicLinkForm } from "@/components/auth-forms";

export const metadata = { title: "Sign in — Metron" };

export default function LoginPage() {
  return (
    <div className="px-4 sm:px-0">
      <MagicLinkForm />
      <p className="mt-6 text-center text-sm text-muted">
        Just looking?{" "}
        <Link href="/demo" className="font-medium text-accent hover:underline">
          Explore the live demo
        </Link>{" "}
        — read-only, no signup.
      </p>
    </div>
  );
}
