import type { OnboardingStepGuideCopy } from "./steps";

export function StepGuide(props: { guide: OnboardingStepGuideCopy }) {
  if (!props.guide.paragraphs.length && !props.guide.title) return null;
  return (
    <div className="mb-5 space-y-2">
      {props.guide.title ? (
        <h3 className="text-[18px] font-headline font-semibold tracking-tight text-white">{props.guide.title}</h3>
      ) : null}
      {props.guide.paragraphs.map((p) => (
        <p key={p} className="text-[14px] leading-relaxed text-slate-300">
          {p}
        </p>
      ))}
    </div>
  );
}
