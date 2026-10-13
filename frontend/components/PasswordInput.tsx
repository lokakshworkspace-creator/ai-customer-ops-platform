"use client";

import { useState } from "react";

// A password field with a Show / Hide button. Shared by the two login forms. No logic beyond
// switching the input's type; the password is only ever sent to /api/v1/auth/login. (Edge draws its
// own reveal icon in password fields: globals.css hides it so there is one control, not two.)
export function PasswordInput(props: {
  value: string;
  onChange: (value: string) => void;
  autoComplete?: string;
  placeholder?: string;
}) {
  const [visible, setVisible] = useState(false);
  return (
    <div className="relative">
      <input
        className="w-full rounded border border-slate-300 py-2 pl-3 pr-16"
        placeholder={props.placeholder ?? "Password"}
        type={visible ? "text" : "password"}
        autoComplete={props.autoComplete ?? "current-password"}
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
        required
      />
      <button
        type="button"
        className="absolute inset-y-0 right-0 px-3 text-sm text-slate-600 underline"
        aria-label={visible ? "Hide password" : "Show password"}
        aria-pressed={visible}
        onClick={() => setVisible((v) => !v)}
      >
        {visible ? "Hide" : "Show"}
      </button>
    </div>
  );
}
