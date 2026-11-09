import React from "react";
import ReactDOM from "react-dom/client";
import { Auth0Provider } from "@auth0/auth0-react";
import App from "./App";
import { AUTH0_CONFIGURED, auth0ProviderProps } from "./auth/config";
import "./index.css";

const tree = (
  <React.StrictMode>
    {AUTH0_CONFIGURED ? (
      <Auth0Provider {...auth0ProviderProps}>
        <App />
      </Auth0Provider>
    ) : (
      <App />
    )}
  </React.StrictMode>
);

ReactDOM.createRoot(document.getElementById("root")!).render(tree);
