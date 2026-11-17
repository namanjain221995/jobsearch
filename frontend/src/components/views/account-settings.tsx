"use client";

import { Monitor, Moon, Sun } from "lucide-react";

import { AccountForm, ChangePasswordForm } from "@/components/people/account-forms";
import { PageHeader, Panel } from "@/components/shared/page-header";
import { SegmentedFilter } from "@/components/shared/filters";
import { useTheme, type ThemeChoice } from "@/lib/theme";

/** Light, dark, or whatever the device uses. Saved in this browser. */
export function AppearancePanel() {
  const { theme, setTheme } = useTheme();
  return (
    <Panel title="Appearance" description="Saved in this browser only.">
      <SegmentedFilter
        label="Theme"
        value={theme}
        onChange={(value) => setTheme(value as ThemeChoice)}
        options={[
          {
            value: "light",
            label: (
              <>
                <Sun aria-hidden="true" /> Light
              </>
            ),
          },
          {
            value: "dark",
            label: (
              <>
                <Moon aria-hidden="true" /> Dark
              </>
            ),
          },
          {
            value: "system",
            label: (
              <>
                <Monitor aria-hidden="true" /> Match device
              </>
            ),
          },
        ]}
      />
    </Panel>
  );
}

/** The parts of "Settings" every signed-in person has: their name, their password, the theme. */
export function AccountPanels() {
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <div className="flex min-w-0 flex-col gap-5">
        <Panel title="Account">
          <AccountForm />
        </Panel>
        <AppearancePanel />
      </div>
      <Panel title="Password" description="Changing it signs you out on your other devices.">
        <ChangePasswordForm />
      </Panel>
    </div>
  );
}

/** Team Lead "Settings": their own account. Portal-wide settings belong to the Admin. */
export function TeamLeadSettingsView() {
  return (
    <>
      <PageHeader
        title="Settings"
        description="Your own account. Limits for searches and other portal-wide settings are managed by an Admin."
      />
      <AccountPanels />
    </>
  );
}
