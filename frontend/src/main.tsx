import React from "react";
import ReactDOM from "react-dom/client";
import App from "./ManualApp";
import Reader from "./Reader";
import PublicApp from "./PublicApp";
import type { ReaderReport } from "./reader-types";
import "./theme.css";
import "./reader.css";

const embedded = document.getElementById("buna-report-data");
const report: ReaderReport | null = embedded ? JSON.parse(embedded.textContent || "null") : null;
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>{import.meta.env.VITE_PUBLIC_MODE === "true" ? <PublicApp /> : report ? <Reader report={report} standalone /> : <App />}</React.StrictMode>,
);
if (report) document.documentElement.classList.add("interactive-ready");
