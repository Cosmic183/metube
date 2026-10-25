import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Subject } from 'rxjs';

export interface AuthUser {
  username: string;
  role: 'admin' | 'user';
}

export interface AuthStatus {
  enabled: boolean;
  authenticated: boolean;
  user: AuthUser | null;
}

@Injectable({ providedIn: 'root' })
export class AuthService {
  private http = inject(HttpClient);

  checked = false;
  enabled = true;
  authenticated = false;
  user: AuthUser | null = null;

  openPasswordModal$ = new Subject<void>();
  authStatusChanged$ = new Subject<void>();

  loadAuthStatus(onComplete?: () => void) {
    // Use relative paths to utilize MeTube's built-in URL_PREFIX interceptor
    this.http.get<AuthStatus>('auth/me').subscribe({
      next: (state) => {
        this.enabled = !!state.enabled;
        this.authenticated = !!state.authenticated;
        this.user = state.user;
        this.checked = true;
        this.authStatusChanged$.next();
        if (onComplete) onComplete();
      },
      error: () => {
        this.enabled = false;
        this.authenticated = true;
        this.user = null;
        this.checked = true;
        this.authStatusChanged$.next();
        if (onComplete) onComplete();
      }
    });
  }

  logout() {
    this.http.post('auth/logout', {}).subscribe({
      next: () => window.location.reload(),
      error: () => window.location.reload()
    });
  }

  triggerPasswordModal() {
    this.openPasswordModal$.next();
  }
}
