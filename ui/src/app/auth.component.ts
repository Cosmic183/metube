import { Component, inject, OnInit, ChangeDetectorRef } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { HttpClient, HttpErrorResponse, HttpParams } from '@angular/common/http';
import { AuthService, AuthUser } from './auth.service';

interface AuthResult {
  status: string;
  msg?: string;
  user?: AuthUser;
  deleted_downloads?: boolean;
  force_relogin?: boolean;
}

@Component({
  selector: 'app-auth',
  standalone: true,
  imports: [FormsModule],
  template: `
    @if (!auth.checked) {
      <div class="text-center mt-5">
        <div class="spinner-border text-primary" role="status"></div>
        <div class="mt-2 text-muted">Securing connection...</div>
      </div>
    } @else if (auth.enabled && !auth.authenticated) {
      <div class="row justify-content-center mt-5">
        <div class="col-12 col-md-6 col-lg-5">
          <div class="card shadow-sm">
            <div class="card-header">Sign in</div>
            <div class="card-body">
              <div class="mb-3">
                <label class="form-label" for="login-username">Username</label>
                <input id="login-username" type="text" class="form-control" [(ngModel)]="loginUsername"
                  name="loginUsername" autocomplete="username" />
              </div>
              <div class="mb-3">
                <label class="form-label" for="login-password">Password</label>
                <input id="login-password" type="password" class="form-control" [(ngModel)]="loginPassword"
                  name="loginPassword" autocomplete="current-password" />
              </div>
              @if (loginError) {
                <div class="text-danger mb-3">{{ loginError }}</div>
              }
              <button type="button" class="btn btn-primary w-100" (click)="login()" [disabled]="loginInProgress">
                {{ loginInProgress ? 'Signing in...' : 'Sign in' }}
              </button>
            </div>
          </div>
        </div>
      </div>
    }

    @if (isAdmin()) {
      <div class="card my-4">
        <div class="card-header">User Administration</div>
        <div class="card-body">
          <div class="row g-2">
            <div class="col-md-4">
              <input type="text" class="form-control" placeholder="Username" [(ngModel)]="newUsername"
                name="newUsername" autocomplete="off" />
            </div>
            <div class="col-md-4">
              <input type="password" class="form-control" placeholder="Password" [(ngModel)]="newPassword"
                name="newPassword" autocomplete="new-password" />
            </div>
            <div class="col-md-2">
              <select class="form-select" [(ngModel)]="newUserRole" name="newUserRole">
                <option [ngValue]="'user'">User</option>
                <option [ngValue]="'admin'">Admin</option>
              </select>
            </div>
            <div class="col-md-2 d-grid">
              <button type="button" class="btn btn-primary" (click)="createUser()" [disabled]="userCreateInProgress">
                {{ userCreateInProgress ? 'Creating...' : 'Create' }}
              </button>
            </div>
          </div>
          @if (userCreateError) { <div class="text-danger mt-2">{{ userCreateError }}</div> }
          @if (userCreateSuccess) { <div class="text-success mt-2">{{ userCreateSuccess }}</div> }
          @if (userDeleteError) { <div class="text-danger mt-2">{{ userDeleteError }}</div> }
          @if (userDeleteSuccess) { <div class="text-success mt-2">{{ userDeleteSuccess }}</div> }

          <hr>
          <div class="small text-muted mb-2">Existing users</div>
          <ul class="list-group">
            @for (user of users; track user.username) {
              <li class="list-group-item d-flex justify-content-between align-items-center">
                <div>
                  <span>{{ user.username }}</span>
                  @if (isCurrentUser(user.username)) { <span class="small text-muted ms-2">(current)</span> }
                </div>
                <div class="d-flex align-items-center gap-2">
                  <span class="badge text-bg-secondary">{{ user.role }}</span>
                  <button type="button" class="btn btn-sm btn-outline-secondary"
                    (click)="openPasswordModalForUser(user)"
                    [disabled]="isDeletingUser(user.username)">
                    Password
                  </button>
                  <button type="button" class="btn btn-sm btn-outline-danger"
                    (click)="openDeleteUserModal(user)"
                    [disabled]="isCurrentUser(user.username) || isDeletingUser(user.username)">
                    {{ isDeletingUser(user.username) ? 'Deleting...' : 'Delete' }}
                  </button>
                </div>
              </li>
            }
          </ul>
        </div>
      </div>
    }

    <!-- Delete User Modal -->
    <div class="modal fade" tabindex="-1" role="dialog"
    [class.show]="deleteUserModalOpen"
    [style.display]="deleteUserModalOpen ? 'block' : 'none'">
      <div class="modal-dialog" role="document">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title">Delete User</h5>
            <button type="button" class="btn-close" aria-label="Close" (click)="closeDeleteUserModal()"></button>
          </div>
          <div class="modal-body">
            @if (deleteUserCandidate) {
              <p>Delete user <strong>{{ deleteUserCandidate.username }}</strong>?</p>
              <div class="form-check">
                <input class="form-check-input" type="checkbox" id="delete-user-downloads"
                  [(ngModel)]="deleteUserWithDownloads">
                <label class="form-check-label" for="delete-user-downloads">
                  Also delete this user's downloads and history
                </label>
              </div>
            }
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" (click)="closeDeleteUserModal()">Cancel</button>
            <button type="button" class="btn btn-danger" (click)="confirmDeleteUser()"
              [disabled]="!deleteUserCandidate || isDeletingUser(deleteUserCandidate.username)">
              @if (deleteUserCandidate && isDeletingUser(deleteUserCandidate.username)) { Deleting... } @else { Delete User }
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Change Password Modal -->
    <div class="modal fade" tabindex="-1" role="dialog"
    [class.show]="passwordModalOpen"
    [style.display]="passwordModalOpen ? 'block' : 'none'">
      <div class="modal-dialog" role="document">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title">Change Password</h5>
            <button type="button" class="btn-close" aria-label="Close" (click)="closePasswordModal()"></button>
          </div>
          <div class="modal-body">
            @if (passwordTargetUser) {
              <p class="mb-3">Set new password for <strong>{{ passwordTargetUser.username }}</strong>.</p>
            }
            @if (passwordRequiresCurrent()) {
              <div class="mb-3">
                <label class="form-label" for="password-current">Current password</label>
                <input id="password-current" type="password" class="form-control"
                  [(ngModel)]="passwordCurrent" autocomplete="current-password">
              </div>
            }
            <div class="mb-3">
              <label class="form-label" for="password-new">New password</label>
              <input id="password-new" type="password" class="form-control"
                [(ngModel)]="passwordNew" autocomplete="new-password">
            </div>
            <div class="mb-3">
              <label class="form-label" for="password-confirm">Confirm new password</label>
              <input id="password-confirm" type="password" class="form-control"
                [(ngModel)]="passwordConfirm" autocomplete="new-password">
            </div>
            @if (passwordError) { <div class="text-danger">{{ passwordError }}</div> }
            @if (passwordSuccess) { <div class="text-success">{{ passwordSuccess }}</div> }
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" (click)="closePasswordModal()">Close</button>
            <button type="button" class="btn btn-primary" (click)="submitPasswordChange()"
              [disabled]="passwordInProgress || !passwordTargetUser">
              {{ passwordInProgress ? 'Updating...' : 'Update Password' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  `
})
export class AuthComponent implements OnInit {
  auth = inject(AuthService);
  private http = inject(HttpClient);
  private cdr = inject(ChangeDetectorRef);

  loginUsername = '';
  loginPassword = '';
  loginInProgress = false;
  loginError = '';
  users: AuthUser[] = [];
  newUsername = '';
  newPassword = '';
  newUserRole: 'admin' | 'user' = 'user';
  userCreateInProgress = false;
  userCreateError = '';
  userCreateSuccess = '';
  deletingUsers = new Set<string>();
  userDeleteError = '';
  userDeleteSuccess = '';
  deleteUserModalOpen = false;
  deleteUserCandidate: AuthUser | null = null;
  deleteUserWithDownloads = false;
  passwordModalOpen = false;
  passwordTargetUser: AuthUser | null = null;
  passwordCurrent = '';
  passwordNew = '';
  passwordConfirm = '';
  passwordInProgress = false;
  passwordError = '';
  passwordSuccess = '';

  ngOnInit() {
    this.auth.openPasswordModal$.subscribe(() => {
      this.openOwnPasswordModal();
      this.cdr.markForCheck();
    });
    this.auth.authStatusChanged$.subscribe(() => {
      if (this.isAdmin()) this.loadUsers();
      this.cdr.markForCheck();
    });
  }

  isAdmin() { return this.auth.user?.role === 'admin'; }
  isCurrentUser(username: string) { return this.auth.user?.username === username; }
  isDeletingUser(username: string) { return this.deletingUsers.has(username); }

  login() {
    this.loginError = '';
    if (this.loginUsername.trim().length === 0 || this.loginPassword.length === 0) {
      this.loginError = 'Username and password are required.';
      return;
    }
    this.loginInProgress = true;
    this.http.post<AuthResult>('auth/login', { username: this.loginUsername.trim(), password: this.loginPassword }).subscribe({
      next: (result) => {
        if (result.status === 'ok') return window.location.reload();
        this.loginError = result.msg || 'Login failed.';
        this.loginInProgress = false;
        this.cdr.markForCheck();
      },
      error: (error: HttpErrorResponse) => {
        this.loginError = error.error?.msg || 'Invalid username or password.';
        this.loginInProgress = false;
        this.cdr.markForCheck();
      }
    });
  }

  loadUsers() {
    if (!this.isAdmin()) return;
    this.http.get<{ users: AuthUser[] }>('auth/users').subscribe({
      next: (res) => { this.users = res.users || []; this.cdr.markForCheck(); },
      error: () => { this.users = []; this.cdr.markForCheck(); }
    });
  }

  createUser() {
    this.userCreateError = ''; this.userCreateSuccess = ''; this.userDeleteError = ''; this.userDeleteSuccess = '';
    if (!this.isAdmin()) return;
    if (this.newUsername.trim().length === 0 || this.newPassword.length === 0) {
      this.userCreateError = 'Username and password are required.';
      return;
    }
    this.userCreateInProgress = true;
    this.http.post<AuthResult>('auth/users', { username: this.newUsername.trim(), password: this.newPassword, role: this.newUserRole }).subscribe({
      next: (result) => {
        if (result.status === 'ok') {
          this.newUsername = ''; this.newPassword = ''; this.newUserRole = 'user';
          this.userCreateSuccess = 'User created.';
          this.loadUsers();
        } else {
          this.userCreateError = result.msg || 'Failed to create user.';
        }
        this.userCreateInProgress = false;
        this.cdr.markForCheck();
      },
      error: (error: HttpErrorResponse) => {
        this.userCreateError = error.error?.msg || 'Failed to create user.';
        this.userCreateInProgress = false;
        this.cdr.markForCheck();
      }
    });
  }

  openDeleteUserModal(user: AuthUser) {
    this.userCreateError = ''; this.userCreateSuccess = ''; this.userDeleteError = ''; this.userDeleteSuccess = '';
    if (!this.isAdmin()) return;
    if (this.isCurrentUser(user.username)) return;
    if (this.isDeletingUser(user.username)) return;
    this.deleteUserCandidate = user;
    this.deleteUserWithDownloads = false;
    this.deleteUserModalOpen = true;
  }

  closeDeleteUserModal() {
    if (this.deleteUserCandidate && this.isDeletingUser(this.deleteUserCandidate.username)) return;
    this.deleteUserModalOpen = false;
    this.deleteUserCandidate = null;
    this.deleteUserWithDownloads = false;
  }

  confirmDeleteUser() {
    if (!this.deleteUserCandidate) return;
    const username = this.deleteUserCandidate.username;
    this.deletingUsers.add(username);
    const params = new HttpParams().set('delete_downloads', this.deleteUserWithDownloads ? 'true' : 'false');
    this.http.delete<AuthResult>(`auth/users/${encodeURIComponent(username)}`, { params }).subscribe({
      next: (result) => {
        if (result.status === 'ok') {
          this.userDeleteSuccess = `User "${username}" deleted.`;
          this.loadUsers();
          this.deletingUsers.delete(username);
          this.closeDeleteUserModal();
        } else {
          this.userDeleteError = result.msg || 'Failed to delete user.';
          this.deletingUsers.delete(username);
        }
        this.cdr.markForCheck();
      },
      error: (error: HttpErrorResponse) => {
        this.userDeleteError = error.error?.msg || 'Failed to delete user.';
        this.deletingUsers.delete(username);
        this.cdr.markForCheck();
      }
    });
  }

  openOwnPasswordModal() {
    if (this.auth.user) this.openPasswordModalForUser(this.auth.user);
  }

  openPasswordModalForUser(user: AuthUser) {
    this.passwordError = ''; this.passwordSuccess = '';
    if (!this.auth.user) return;
    if (!this.isAdmin() && user.username !== this.auth.user.username) return;
    this.passwordTargetUser = user;
    this.passwordModalOpen = true;
    this.passwordCurrent = ''; this.passwordNew = ''; this.passwordConfirm = '';
  }

  closePasswordModal() {
    if (this.passwordInProgress) return;
    this.passwordModalOpen = false;
    this.passwordTargetUser = null;
    this.passwordCurrent = ''; this.passwordNew = ''; this.passwordConfirm = '';
    this.passwordError = ''; this.passwordSuccess = '';
  }

  passwordRequiresCurrent() {
    if (!this.passwordTargetUser || !this.auth.user) return false;
    return (!this.isAdmin()) && this.passwordTargetUser.username === this.auth.user.username;
  }

  submitPasswordChange() {
    this.passwordError = ''; this.passwordSuccess = '';
    if (!this.passwordTargetUser) return;
    if (!this.passwordNew || !this.passwordConfirm) { this.passwordError = 'New password and confirmation are required.'; return; }
    if (this.passwordNew !== this.passwordConfirm) { this.passwordError = 'New password and confirmation do not match.'; return; }
    if (this.passwordRequiresCurrent() && !this.passwordCurrent) { this.passwordError = 'Current password is required.'; return; }

    const body: { new_password: string; current_password?: string } = { new_password: this.passwordNew };
    if (this.passwordRequiresCurrent()) body.current_password = this.passwordCurrent;

    const username = this.passwordTargetUser.username;
    this.passwordInProgress = true;
    this.http.post<AuthResult>(`auth/users/${encodeURIComponent(username)}/password`, body).subscribe({
      next: (result) => {
        if (result.status === 'ok') {
          if (result.force_relogin) return window.location.reload();
          this.passwordSuccess = `Password updated for "${username}".`;
          this.passwordCurrent = ''; this.passwordNew = ''; this.passwordConfirm = '';
          this.loadUsers();
        } else {
          this.passwordError = result.msg || 'Failed to change password.';
        }
        this.passwordInProgress = false;
        this.cdr.markForCheck();
      },
      error: (error: HttpErrorResponse) => {
        this.passwordError = error.error?.msg || 'Failed to change password.';
        this.passwordInProgress = false;
        this.cdr.markForCheck();
      }
    });
  }
}
