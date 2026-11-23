"use client";

import { useEffect } from "react";

/**
 * The landing page's one piece of shared motion code, rendered once.
 *
 * - Reveals: every element marked `data-reveal` gets `data-shown` the first time it
 *   scrolls into view. The CSS hides those elements only while `html.lp-js` is set, so
 *   without JavaScript everything is simply visible.
 * - Parallax: the scroll position (capped) and, in the hero, the pointer position are
 *   written as CSS variables on the landing root; the CSS turns them into small shifts.
 *   Neither is written when the visitor prefers reduced motion.
 */
export function LandingMotion() {
  useEffect(() => {
    const root = document.querySelector<HTMLElement>(".lp-root");
    if (!root) return;
    const html = document.documentElement;
    html.classList.add("lp-js");
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          entry.target.setAttribute("data-shown", "");
          io.unobserve(entry.target);
        }
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0 },
    );
    root.querySelectorAll("[data-reveal]").forEach((el) => io.observe(el));

    let frame = 0;
    const onScroll = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        const y = window.scrollY;
        root.toggleAttribute("data-scrolled", y > 8);
        if (!reduce) root.style.setProperty("--lp-sy", String(Math.min(Math.round(y), 1400)));
      });
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });

    const hero = root.querySelector<HTMLElement>(".lp-hero");
    let pointerFrame = 0;
    const onPointer = (event: PointerEvent) => {
      if (event.pointerType !== "mouse" || !hero || pointerFrame) return;
      pointerFrame = window.requestAnimationFrame(() => {
        pointerFrame = 0;
        const box = hero.getBoundingClientRect();
        const x = ((event.clientX - box.left) / box.width) * 2 - 1;
        const y = ((event.clientY - box.top) / box.height) * 2 - 1;
        root.style.setProperty("--lp-px", x.toFixed(3));
        root.style.setProperty("--lp-py", y.toFixed(3));
      });
    };
    if (!reduce) hero?.addEventListener("pointermove", onPointer);

    return () => {
      io.disconnect();
      window.removeEventListener("scroll", onScroll);
      hero?.removeEventListener("pointermove", onPointer);
      window.cancelAnimationFrame(frame);
      window.cancelAnimationFrame(pointerFrame);
      html.classList.remove("lp-js");
    };
  }, []);

  return null;
}
