import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { BehaviorSubject } from 'rxjs';

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
  
  authEnabled = new BehaviorSubject<boolean>(false);
  authenticated = new BehaviorSubject<boolean>(false);
  authUser = new BehaviorSubject<AuthUser | null>(null);
  authChecked = new BehaviorSubject<boolean>(false);

  loadAuthStatus(onComplete?: () => void) {
    this.http.get<AuthStatus>('auth/me').subscribe({
      next: (state) => {
        this.authEnabled.next(!!state.enabled);
        this.authenticated.next(!!state.authenticated);
        this.authUser.next(state.user);
        this.authChecked.next(true);
        if (onComplete) onComplete();
      },
      error: () => {
        this.authEnabled.next(false);
        this.authenticated.next(true);
        this.authUser.next(null);
        this.authChecked.next(true);
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
}
