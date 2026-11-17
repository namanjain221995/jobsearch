import type { Metadata } from "next";
import { Suspense } from "react";

import { ResetPasswordForm } from "@/components/auth/reset-password-form";
import { LoadingSkeleton } from "@/components/shared/states";

export const metadata: Metadata = { title: "Choose a new password" };

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={<LoadingSkeleton variant="form" count={3} />}>
      <ResetPasswordForm />
    </Suspense>
  );
}
