import { Component, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { FormsModule } from '@angular/forms';

@Component({
  selector: 'app-auth-login',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="row justify-content-center mt-5">
      <div class="col-12 col-md-6 col-lg-5">
        <div class="card shadow-sm">
          <div class="card-header">Sign in</div>
          <div class="card-body">
            <div class="mb-3">
              <label class="form-label" for="login-username">Username</label>
              <input id="login-username" type="text" class="form-control" [(ngModel)]="username" name="username" autocomplete="username" />
            </div>
            <div class="mb-3">
              <label class="form-label" for="login-password">Password</label>
              <input id="login-password" type="password" class="form-control" [(ngModel)]="password" name="password" autocomplete="current-password" />
            </div>
            @if (error) { <div class="text-danger mb-3">{{ error }}</div> }
            <button type="button" class="btn btn-primary w-100" (click)="login()" [disabled]="loading">
              {{ loading ? 'Signing in...' : 'Sign in' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  `
})
export class AuthLoginComponent {
  private http = inject(HttpClient);
  username = '';
  password = '';
  loading = false;
  error = '';

  login() {
    this.error = '';
    if (!this.username.trim() || !this.password) {
      this.error = 'Username and password are required.';
      return;
    }
    this.loading = true;
    this.http.post<any>('auth/login', { username: this.username.trim(), password: this.password }).subscribe({
      next: (res) => {
        if (res.status === 'ok') window.location.reload();
        else { this.error = res.msg || 'Login failed.'; this.loading = false; }
      },
      error: (err) => {
        this.error = err.error?.msg || 'Invalid username or password.';
        this.loading = false;
      }
    });
  }
}
