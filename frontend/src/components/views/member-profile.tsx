"use client";

import { AccountForm, ChangePasswordForm } from "@/components/people/account-forms";
import { ProfileCompletion, ProfileForm } from "@/components/people/profile-form";
import { ResumeUpload } from "@/components/people/resume-upload";
import { PageHeader, Panel } from "@/components/shared/page-header";
import { ErrorState, LoadingSkeleton } from "@/components/shared/states";
import { useMe } from "@/components/shell/me-context";
import { useProfile } from "@/lib/queries";

function Completion({ memberId }: { memberId: string }) {
  const profile = useProfile(memberId);
  if (profile.isPending) return <LoadingSkeleton variant="form" count={1} />;
  if (profile.isError) return <ErrorState compact error={profile.error} onRetry={() => profile.refetch()} />;
  return <ProfileCompletion profile={profile.data} />;
}

export function MemberProfileView() {
  const me = useMe();
  return (
    <>
      <PageHeader
        title="Profile"
        description="Your Team Lead searches for jobs using this profile and your resume. The more complete it is, the better the matches."
      />
      {/* Small screens: completion, preferences, resume, then account. Wide screens: account in a side column. */}
      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_22rem] xl:grid-rows-[auto_1fr]">
        <Panel title="Profile completion" className="xl:col-start-2 xl:row-start-1">
          <Completion memberId={me.id} />
        </Panel>
        <div className="flex min-w-0 flex-col gap-5 xl:col-start-1 xl:row-span-2 xl:row-start-1">
          <Panel title="Job preferences">
            <ProfileForm memberId={me.id} />
          </Panel>
          <Panel title="Resume" description="PDF, DOCX or TXT. Uploading a new file replaces the current one.">
            <ResumeUpload memberId={me.id} />
          </Panel>
        </div>
        <div className="flex min-w-0 flex-col gap-5 xl:col-start-2 xl:row-start-2">
          <Panel title="Account">
            <AccountForm />
          </Panel>
          <Panel title="Password" description="Changing it signs you out on your other devices.">
            <ChangePasswordForm />
          </Panel>
        </div>
      </div>
    </>
  );
}
