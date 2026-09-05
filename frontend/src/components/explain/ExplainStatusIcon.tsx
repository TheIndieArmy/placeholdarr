export function ExplainStatusIcon(props: { status: "pass" | "fail" | "skip" | "applied" }) {
  if (props.status === "pass") {
    return (
      <span className="material-symbols-outlined text-emerald-400" style={{ fontSize: 16 }}>
        check_circle
      </span>
    );
  }
  if (props.status === "fail") {
    return (
      <span className="material-symbols-outlined text-red-400" style={{ fontSize: 16 }}>
        cancel
      </span>
    );
  }
  if (props.status === "applied") {
    return (
      <span className="material-symbols-outlined text-amber-400" style={{ fontSize: 16 }}>
        change_circle
      </span>
    );
  }
  return (
    <span className="material-symbols-outlined text-slate-600" style={{ fontSize: 16 }}>
      radio_button_unchecked
    </span>
  );
}
