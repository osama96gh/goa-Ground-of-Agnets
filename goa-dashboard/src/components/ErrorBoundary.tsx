import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

// Catches render-time crashes that the query-cache 401 path doesn't.
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[error-boundary]", error, info);
  }

  reset = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      return (
        <div className="flex min-h-[60vh] items-center justify-center p-6">
          <Card className="max-w-md">
            <CardContent className="flex flex-col items-center gap-4 p-8 text-center">
              <div className="flex h-12 w-12 items-center justify-center rounded-full bg-destructive/10 text-destructive">
                <AlertTriangle className="h-6 w-6" />
              </div>
              <div>
                <h2 className="text-base font-semibold">Something broke</h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  {this.state.error.message || "An unexpected error occurred."}
                </p>
              </div>
              <div className="flex gap-2">
                <Button variant="outline" onClick={this.reset}>
                  Try again
                </Button>
                <Button onClick={() => window.location.reload()}>
                  Reload
                </Button>
              </div>
            </CardContent>
          </Card>
        </div>
      );
    }
    return this.props.children;
  }
}
