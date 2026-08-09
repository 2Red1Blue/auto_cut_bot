"use client";

import { useState } from "react";
import { useClient } from "@/providers/client-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ShieldCheck, KeyRound, Loader2 } from "lucide-react";

export function AuthScreen() {
  const { login, status, error } = useClient();
  const [secret, setSecret] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!secret.trim()) return;
    setSubmitting(true);
    try {
      await login(secret.trim());
    } finally {
      setSubmitting(false);
    }
  };

  const isLoading = status === "connecting" || submitting;

  return (
    <div className="flex h-screen items-center justify-center bg-background">
      <div className="w-full max-w-sm space-y-6 p-8">
        <div className="text-center space-y-2">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-primary/10">
            <ShieldCheck className="h-6 w-6 text-primary" />
          </div>
          <h1 className="text-2xl font-semibold">Auto Cut Bot</h1>
          <p className="text-sm text-muted-foreground">
            Enter your bootstrap secret to connect to the agent.
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <label className="text-sm font-medium" htmlFor="secret">
              Bootstrap Secret
            </label>
            <div className="relative">
              <KeyRound className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                id="secret"
                type="password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder="Enter your secret key..."
                className="pl-10"
                disabled={isLoading}
                autoFocus
              />
            </div>
          </div>

          {error && (
            <p className="text-sm text-destructive">{error}</p>
          )}

          <Button type="submit" className="w-full" disabled={isLoading || !secret.trim()}>
            {isLoading ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Connecting...
              </>
            ) : (
              "Connect"
            )}
          </Button>
        </form>

        <p className="text-xs text-muted-foreground text-center">
          You can find the bootstrap secret in the gateway logs or ask your administrator.
        </p>
      </div>
    </div>
  );
}