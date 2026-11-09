/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Auth0 tenant domain — when set, the app uses Auth0 Universal Login. */
  readonly VITE_AUTH0_DOMAIN?: string;
  readonly VITE_AUTH0_CLIENT_ID?: string;
  readonly VITE_AUTH0_AUDIENCE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
