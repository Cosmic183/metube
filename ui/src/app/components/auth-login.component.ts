/* eslint-disable @typescript-eslint/no-explicit-any */
import { Component, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AuthService } from '../services/auth.service';

@Component({
  selector: 'app-auth-login',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="login-container">
      <h2>MeTube Login</h2>
      <div class="form-group mb-3">
        <label for="usernameInput">Username</label>
        <input id="usernameInput" class="form-control" [(ngModel)]="username" (keyup.enter)="login()">
      </div>
      <div class="form-group mb-3">
        <label for="passwordInput">Password</label>
        <input id="passwordInput" class="form-control" type="password" [(ngModel)]="password" (keyup.enter)="login()">
      </div>
      @if (error) {
        <div class="text-danger mb-3">{{ error }}</div>
      }
      <button class="btn btn-primary w-100" (click)="login()" [disabled]="!username || !password">Login</button>
    </div>
  `,
  styles: [`
    .login-container { max-width: 400px; margin: 4rem auto; padding: 2rem; border-radius: 8px; text-align: center; background: var(--bs-body-bg); box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
  `]
})
export class AuthLoginComponent {
  
  // THIS IS THE EXACT LINE THAT FIXES THE ERROR
  authService: any = inject(AuthService);
  
  username = '';
  password = '';
  error = '';

  login() {
    this.authService.login(this.username, this.password).subscribe({
      next: () => this.error = '',
      error: () => this.error = 'Invalid username or password.'
    });
  }
}
