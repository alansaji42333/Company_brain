// Auth0 Universal Login (Authorization Code + PKCE) configuration.
//
// Production: set VITE_AUTH0_DOMAIN + VITE_AUTH0_CLIENT_ID (+
// VITE_AUTH0_AUDIENCE matching the backend's AUTH0_AUDIENCE so the API can
// verify the issued access tokens).
// Dev mode: with VITE_AUTH0_DOMAIN unset (or no client id) the app uses the
// paste-a-token flow instead — the Auth0 SDK is never mounted and tokens
// stay in localStorage.

const AUTH0_DOMAIN = import.meta.env.VITE_AUTH0_DOMAIN ?? "";
const AUTH0_CLIENT_ID = import.meta.env.VITE_AUTH0_CLIENT_ID ?? "";
const AUTH0_AUDIENCE = import.meta.env.VITE_AUTH0_AUDIENCE ?? "";

/** True when the frontend is configured for Auth0 Universal Login. */
export const AUTH0_CONFIGURED = Boolean(AUTH0_DOMAIN && AUTH0_CLIENT_ID);

export const auth0ProviderProps = {
  domain: AUTH0_DOMAIN,
  clientId: AUTH0_CLIENT_ID,
  authorizationParams: {
    redirect_uri: window.location.origin,
    ...(AUTH0_AUDIENCE ? { audience: AUTH0_AUDIENCE } : {}),
    scope: "openid profile email",
  },
};
