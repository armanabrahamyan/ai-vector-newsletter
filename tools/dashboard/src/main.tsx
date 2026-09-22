import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles.css";

// No StrictMode: its dev-mode double-mount fires every effect twice, which
// doubles the API poll and trips GoatCounter's rate limit on first load.
ReactDOM.createRoot(document.getElementById("root")!).render(<App />);
