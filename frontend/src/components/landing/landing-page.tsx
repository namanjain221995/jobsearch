import Link from "next/link";
import { Check } from "lucide-react";

import "./landing.css";

import { BrandLockup, BrandMark } from "@/components/brand";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { AgentOrbit, AutoApplyForm, MatchCard, SalesforceFeed, TeamRooms, TrackingRails, VerifyBrowser } from "./graphics";
import { HeroScene } from "./hero-scene";
import { HowSteps } from "./how-steps";
import { LandingMotion } from "./landing-motion";
import { MotionPauseButton } from "./motion-pause";

/**
 * The public front page, shown at "/" to visitors who are not signed in (the proxy
 * sends signed-in people to their dashboard before this renders).
 *
 * Everything here is true of the product. The illustrations are labelled or plainly
 * mock-ups, and no figure is shown that the product does not guarantee.
 */

const FACTS = [
  "US jobs only",
  "Every job checked on the employer's live page",
  "A 0 to 100 match score with a written reason",
  "Each team sees only its own candidates",
  "Nothing generated to fill a quota",
  "Auto-apply waits for an Admin's approval",
  "Candidates imported from Salesforce, read-only",
];

const AGENT_ROLES = [
  { name: "Discovery", body: "Searches the open web and career sites with many queries at once." },
  { name: "Reader", body: "Opens each posting and pulls out the details that matter." },
  { name: "Verifier", body: "Confirms the page is live, still open and in the US." },
  { name: "Ranker", body: "Reads every job against the resume and scores the fit." },
  { name: "Explainer", body: "Writes the reason behind each score in plain words." },
];

const VERIFY_CHECKS = [
  "The employer's page loads",
  "The posting is still open",
  "The job is in the United States",
  "The details match what was found",
];

const ROLES = [
  {
    name: "Admin",
    body: "Creates teams and Team Leads, sets search limits and sources, and sees the numbers for the whole organization.",
  },
  {
    name: "Team Lead",
    body: "Invites recruiters, adds candidates and their resumes, runs searches and follows the team's progress.",
  },
  {
    name: "Recruiter",
    body: "Works the candidates assigned to them: reads each job, opens the application page and records what was applied for.",
  },
];

function SignInButton({ className, size = "lg" }: { className?: string; size?: "lg" | "default" }) {
  return (
    <Button asChild size={size} className={cn("lp-cta", className)}>
      <Link href="/login">Sign in</Link>
    </Button>
  );
}

export function LandingPage() {
  return (
    <div className="lp-root">
      {/* Lets the CSS hide not-yet-revealed blocks before the first paint; without JavaScript nothing is hidden. */}
      <script dangerouslySetInnerHTML={{ __html: "document.documentElement.classList.add('lp-js')" }} />
      <LandingMotion />

      <a href="#main" className="lp-skip">
        Skip to content
      </a>

      <header className="lp-header">
        <div className="lp-wrap flex h-16 items-center justify-between gap-4">
          <Link href="/" className="inline-flex rounded-sm" aria-label="AI Job Finder, home">
            <BrandLockup />
          </Link>
          <nav aria-label="Sections" className="hidden items-center gap-1 md:flex">
            <a href="#how" className="lp-navlink">
              How it works
            </a>
            <a href="#verify" className="lp-navlink">
              Verification
            </a>
            <a href="#teams" className="lp-navlink">
              Teams
            </a>
          </nav>
          <SignInButton size="default" className="h-9 px-4" />
        </div>
      </header>

      <main id="main">
        {/* ------------------------------------------------------------- hero */}
        <section className="lp-hero" aria-labelledby="hero-title">
          <div className="lp-field" aria-hidden="true">
            <div className="lp-field-dots" />
            <div className="lp-field-scan" />
            <div className="lp-field-glow" />
          </div>

          <div className="lp-wrap relative grid grid-cols-[minmax(0,1fr)] items-center gap-14 pt-10 pb-20 sm:pt-16 lg:grid-cols-[minmax(0,0.92fr)_minmax(0,1.08fr)] lg:gap-10 lg:pt-20 lg:pb-28">
            <div className="lp-hero-copy">
              <h1 id="hero-title" className="lp-h1">
                <span className="lp-line">
                  <span style={{ animationDelay: "80ms" }}>Real US jobs,</span>
                </span>
                <span className="lp-line">
                  <span style={{ animationDelay: "200ms" }}>checked live,</span>
                </span>
                <span className="lp-line">
                  <span style={{ animationDelay: "320ms" }}>matched to every candidate.</span>
                </span>
              </h1>
              <p className="lp-rise mt-7 max-w-[34rem] text-lg leading-relaxed text-muted-foreground" style={{ animationDelay: "520ms" }}>
                AI Job Finder sends AI agents across the open web and employers&apos; own career sites, confirms
                every opening on the employer&apos;s live page, and gives your recruiters only the jobs that fit the
                candidate&apos;s resume.
              </p>
              <div className="lp-rise mt-9 flex flex-wrap items-center gap-3" style={{ animationDelay: "640ms" }}>
                <SignInButton className="h-11 px-6 text-md" />
                <Button asChild variant="outline" size="lg" className="lp-ghost h-11 px-5 text-md">
                  <a href="#how">See how a search runs</a>
                </Button>
              </div>
              <p className="lp-rise mt-5 text-2sm text-muted-foreground" style={{ animationDelay: "760ms" }}>
                Accounts are created by invitation from your Admin or Team Lead.
              </p>
              <div className="lp-rise mt-8" style={{ animationDelay: "900ms" }}>
                <MotionPauseButton />
              </div>
            </div>

            <div className="lp-hero-visual lp-rise" style={{ animationDelay: "260ms" }}>
              <HeroScene />
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- facts */}
        <section aria-label="In short" className="lp-facts">
          <ul className="lp-facts-track">
            {FACTS.map((fact) => (
              <li key={fact}>
                <BrandMark className="size-4" />
                {fact}
              </li>
            ))}
          </ul>
          <ul className="lp-facts-track lp-facts-copy" aria-hidden="true">
            {FACTS.map((fact) => (
              <li key={fact}>
                <BrandMark className="size-4" />
                {fact}
              </li>
            ))}
          </ul>
        </section>

        {/* ------------------------------------------------------------- how */}
        <section id="how" className="lp-section" aria-labelledby="how-title">
          <div className="lp-wrap">
            <header className="lp-section-head" data-reveal>
              <h2 id="how-title" className="lp-h2">
                One search, five stages
              </h2>
              <p className="lp-lead">
                Start a search for one candidate or a whole team&apos;s list. It runs on the server, carries on if you
                close the browser, and shows each stage as it happens.
              </p>
            </header>
            <div className="mt-16">
              <HowSteps />
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- agents */}
        <section className="lp-section lp-flush-top" aria-labelledby="agents-title">
          <div className="lp-wrap">
            <div className="lp-deep" data-reveal>
              <div className="lp-deep-glow" aria-hidden="true" />
              <div className="relative grid grid-cols-[minmax(0,1fr)] items-center gap-12 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                <div>
                  <h2 id="agents-title" className="lp-h2">
                    Several AI models, one shortlist
                  </h2>
                  <p className="lp-lead lp-deep-muted">
                    No single model sees everything. Different AI agents search, read, verify, rank and explain, and
                    their judgements land on one 0 to 100 scale, so a score means the same thing whoever produced it.
                  </p>
                  <dl className="lp-agents">
                    {AGENT_ROLES.map((role) => (
                      <div key={role.name}>
                        <dt className="font-display text-lg leading-tight">{role.name}</dt>
                        <dd className="lp-deep-muted text-md">{role.body}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
                <AgentOrbit />
              </div>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- verify */}
        <section id="verify" className="lp-section" aria-labelledby="verify-title">
          <div className="lp-wrap grid grid-cols-[minmax(0,1fr)] items-center gap-14 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,0.95fr)] lg:gap-20">
            <div className="lp-parallax-soft order-2 lg:order-1" data-reveal>
              <VerifyBrowser />
            </div>
            <div className="order-1 lg:order-2" data-reveal>
              <h2 id="verify-title" className="lp-h2">
                If the page is gone, so is the job
              </h2>
              <p className="lp-lead">
                Postings linger long after a role is filled. Before a job reaches a recruiter, AI Job Finder opens the
                employer&apos;s page and checks it.
              </p>
              <ul className="lp-checks">
                {VERIFY_CHECKS.map((item, i) => (
                  <li key={item} style={{ animationDelay: `${300 + i * 260}ms` }}>
                    <span className="lp-check-icon" aria-hidden="true">
                      <Check className="size-3.5" strokeWidth={3} />
                    </span>
                    {item}
                  </li>
                ))}
              </ul>
              <p className="mt-6 max-w-[32rem] text-2sm text-muted-foreground">
                A job an Admin adds by hand is marked unverified until its page has been checked. Nothing is generated
                to fill a quota: if 50 jobs were asked for and 18 qualify, you see 18.
              </p>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- match */}
        <section className="lp-section lp-section-tint" aria-labelledby="match-title">
          <div className="lp-wrap">
            <header className="lp-section-head mx-auto text-center" data-reveal>
              <h2 id="match-title" className="lp-h2">
                A score you can read
              </h2>
              <p className="lp-lead mx-auto">
                Each job is scored against the candidate&apos;s resume and preferences on a 100-point scale. Skills,
                title, experience, work arrangement and the rest each carry a fixed share, and the result says why.
              </p>
            </header>
            <div className="mx-auto mt-14 max-w-[46rem]" data-reveal>
              <MatchCard />
              <p className="lp-scene-caption lp-caption-center">Illustration of a match result</p>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- teams */}
        <section id="teams" className="lp-section" aria-labelledby="teams-title">
          <div className="lp-wrap">
            <header className="lp-section-head" data-reveal>
              <h2 id="teams-title" className="lp-h2">
                Built for a staffing team
              </h2>
              <p className="lp-lead">
                Three roles, each with its own screens. A Team Lead sees only their team; a recruiter sees only the
                candidates assigned to them. The server checks this on every request.
              </p>
            </header>

            <div className="mt-14 grid grid-cols-[minmax(0,1fr)] gap-14 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)] lg:gap-20">
              <dl className="lp-roles" data-reveal>
                {ROLES.map((role) => (
                  <div key={role.name} className="lp-role">
                    <dt className="lp-role-name">{role.name}</dt>
                    <dd className="text-md text-muted-foreground">{role.body}</dd>
                  </div>
                ))}
              </dl>
              <div className="flex flex-col gap-12">
                <figure data-reveal>
                  <TeamRooms />
                  <figcaption className="mt-3 text-2sm text-muted-foreground">
                    Teams are kept apart: candidates, jobs and searches stay inside their own team.
                  </figcaption>
                </figure>
                <figure data-reveal>
                  <TrackingRails />
                  <figcaption className="mt-4 text-2sm text-muted-foreground">
                    Every assigned job is followed from the first look to the offer, on dashboards for the recruiter,
                    the team and the organization.
                  </figcaption>
                </figure>
              </div>
            </div>
          </div>
        </section>

        {/* ------------------------------------------------------------- integrations */}
        <section className="lp-section lp-flush-top" aria-label="Salesforce import and auto-apply">
          <div className="lp-wrap grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-2">
            <article className="lp-panel" data-reveal>
              <SalesforceFeed />
              <h2 className="lp-h3 mt-8">Bring candidates in from Salesforce</h2>
              <p className="mt-3 max-w-[32rem] text-md text-muted-foreground">
                Search by name, e-mail or phone and import the candidate&apos;s details, job-search profile and latest
                resume. The connection only reads: nothing in Salesforce is ever changed.
              </p>
            </article>
            <article className="lp-panel" data-reveal>
              <AutoApplyForm />
              <h2 className="lp-h3 mt-8">Auto-apply, with a person in charge</h2>
              <p className="mt-3 max-w-[32rem] text-md text-muted-foreground">
                An agent fills the employer&apos;s own application form in a dry run first. The Admin sees every field,
                its value and a screenshot, and approves before anything is submitted. Sign-ins, CAPTCHAs and legal
                questions always go back to a person.
              </p>
            </article>
          </div>
        </section>

        {/* ------------------------------------------------------------- final call */}
        <section className="lp-final" aria-labelledby="final-title">
          <div className="lp-field" aria-hidden="true">
            <div className="lp-field-dots" />
            <div className="lp-field-scan" />
          </div>
          <div className="lp-wrap relative flex flex-col items-center text-center" data-reveal>
            <span className="lp-final-mark" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            <h2 id="final-title" className="lp-h2 lp-final-title mt-10">
              Your next shortlist starts with a search
            </h2>
            <p className="lp-lead mx-auto">
              Sign in to search for your candidates. New here? Accounts are created by invitation, so ask your Team Lead
              or Admin.
            </p>
            <SignInButton className="mt-9 h-12 px-8 text-md" />
          </div>
        </section>
      </main>

      <footer className="lp-footer">
        <div className="lp-wrap flex flex-col gap-4 py-10 sm:flex-row sm:items-center sm:justify-between">
          <BrandLockup />
          <p className="text-2sm text-muted-foreground">AI Job Finder by TechSara Solutions</p>
          <nav aria-label="Account" className="flex gap-5 text-2sm">
            <Link href="/login" className="lp-navlink lp-footlink">
              Sign in
            </Link>
            <Link href="/forgot-password" className="lp-navlink lp-footlink">
              Forgot password
            </Link>
          </nav>
        </div>
      </footer>
    </div>
  );
}
