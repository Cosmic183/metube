import { Component, inject } from '@angular/core';
import { AuthService } from '../auth.service';

@Component({
  selector: 'app-auth-login',
  template: `
    <div class="login-container mat-elevation-z4">
      <h2>MeTube Login</h2>
      <div class="form-group">
        <mat-form-field appearance="fill" class="full-width">
          <mat-label>Username</mat-label>
          <input matInput [(ngModel)]="username" (keyup.enter)="login()">
        </mat-form-field>
      </div>
      <div class="form-group">
        <mat-form-field appearance="fill" class="full-width">
          <mat-label>Password</mat-label>
          <input matInput type="password" [(ngModel)]="password" (keyup.enter)="login()">
        </mat-form-field>
      </div>
      @if (error) {
        <div class="error-message">{{ error }}</div>
      }
      <button mat-raised-button color="primary" (click)="login()" [disabled]="!username || !password">Login</button>
    </div>
  `,
  styles: [`
    .login-container { max-width: 400px; margin: 4rem auto; padding: 2rem; border-radius: 8px; text-align: center; background: var(--card-background-color, #fff); }
    .full-width { width: 100%; margin-bottom: 1rem; }
    .error-message { color: #f44336; margin-bottom: 1rem; font-weight: 500; }
  `]
})
export class AuthLoginComponent {
  authService = inject(AuthService);
  
  username = '';
  password = '';
  error = '';

  login() {
    this.authService.login(this.username, this.password).subscribe({
      next: () => {
        this.error = '';
      },
      error: () => {
        this.error = 'Invalid username or password.';
      }
    });
  }
}
