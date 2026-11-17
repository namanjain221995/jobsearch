import { FileText } from "lucide-react";

import { EmptyState } from "@/components/shared/states";
import { parseDescription } from "@/lib/description";
import { cn } from "@/lib/utils";

/**
 * The posting's description. The text is plain text from the API and is only
 * ever placed in the page as text: nothing in it is interpreted as markup.
 * Headings appear only where the text itself has a recognisable heading line.
 */
export function JobDescription({ text, className }: { text: string | null | undefined; className?: string }) {
  const sections = parseDescription(text);
  if (sections.length === 0) {
    return (
      <EmptyState
        compact
        icon={FileText}
        title="No description was stored for this job"
        description="Open the job posting to read the full details."
      />
    );
  }
  return (
    <div className={cn("flex max-w-[72ch] flex-col gap-6 text-md leading-relaxed", className)} data-slot="job-description">
      {sections.map((section, sectionIndex) => (
        <section key={sectionIndex} className="flex flex-col gap-2.5">
          {section.heading && <h4 className="font-label text-lg leading-snug">{section.heading}</h4>}
          {section.blocks.map((block, blockIndex) =>
            block.type === "list" ? (
              <ul key={blockIndex} className="flex flex-col gap-1.5">
                {block.items.map((item, itemIndex) => (
                  <li key={itemIndex} className="relative pl-4.5 before:absolute before:top-[0.68em] before:left-0.5 before:size-1.5 before:rounded-[1px] before:bg-status-viewed">
                    {item}
                  </li>
                ))}
              </ul>
            ) : (
              <p key={blockIndex} className="whitespace-pre-line">
                {block.text}
              </p>
            ),
          )}
        </section>
      ))}
    </div>
  );
}
