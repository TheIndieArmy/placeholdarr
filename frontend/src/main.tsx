import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { ViewportModeProvider } from "./viewport/ViewportModeContext";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ViewportModeProvider>
      {/*
        RR7 defaults to startTransition for location updates. Always-on task
        polls / SSE setState in App can interrupt those transitions so the
        address bar moves while the shell stays on the old page. Keep router
        updates synchronous for this dashboard.
      */}
      <BrowserRouter useTransitions={false}>
        <App />
      </BrowserRouter>
    </ViewportModeProvider>
  </React.StrictMode>
);
