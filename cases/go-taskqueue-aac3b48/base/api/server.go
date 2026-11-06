package api

import (
	"context"
	"encoding/json"
	"net/http"
	"strconv"
	"strings"

	"go-taskqueue/model"
	"go-taskqueue/queue"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type Server struct {
	dbPool *pgxpool.Pool
}

func NewServer(addr string, dbPool *pgxpool.Pool) *http.Server {
	mux := http.NewServeMux()

	srv := &Server{dbPool: dbPool}
	mux.HandleFunc("GET /tasks/{id}", srv.getTask)
	mux.HandleFunc("DELETE /tasks/{id}", srv.cancelTaskByID)
	mux.HandleFunc("GET /tasks", srv.getTasks)
	mux.HandleFunc("POST /tasks", srv.postTask)

	return &http.Server{
		Addr:    addr,
		Handler: mux,
	}
}

func (s *Server) getTask(w http.ResponseWriter, r *http.Request) {
	idStr := r.PathValue("id")
	id, err := strconv.Atoi(idStr)
	if err != nil {
		http.Error(w, "Invalid task ID", http.StatusBadRequest)
		return
	}

	var task model.Task
	err = s.dbPool.QueryRow(context.Background(), `
		SELECT id, type, payload, status, retries, result, created_at, updated_at
		FROM tasks WHERE id = $1`, id).Scan(
		&task.ID, &task.Type, &task.Payload, &task.Status, &task.Retries, &task.Result, &task.CreatedAt, &task.UpdatedAt,
	)
	if err == pgx.ErrNoRows {
		http.Error(w, "[API] Task not found", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(w, "[API] Database error", http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(task); err != nil {
		http.Error(w, "[API] Encoding error", http.StatusInternalServerError)
		return
	}
}

func (s Server) postTask(w http.ResponseWriter, r *http.Request) {
	var task model.Task
	err := json.NewDecoder(r.Body).Decode(&task)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	task.Status = "queued"
	err = s.dbPool.QueryRow(context.Background(), `
		INSERT INTO tasks (type, payload, status, retries)
		VALUES ($1, $2, $3, $4)
		RETURNING id, created_at, updated_at`,
		task.Type, task.Payload, task.Status, task.Retries,
	).Scan(&task.ID, &task.CreatedAt, &task.UpdatedAt)
	if err != nil {
		http.Error(w, "[API] Failed to insert task", http.StatusInternalServerError)
		return
	}

	err = queue.Enqueue(task)
	if err != nil {
		http.Error(w, "[API] Failed to enqueue task", http.StatusInternalServerError)
		return
	}

	w.WriteHeader(http.StatusCreated)
	if err := json.NewEncoder(w).Encode(task); err != nil {
		http.Error(w, "[API] Encoding error", http.StatusInternalServerError)
		return
	}
}

func (s *Server) getTasks(w http.ResponseWriter, r *http.Request) {
	statusFilter := strings.ToLower(r.URL.Query().Get("status"))

	// ADD LIMIT
	// ADD OFFSET

	validValues := map[string]bool{
		"completed":  true,
		"processing": true,
		"queued":     true,
		"failed":     true,
		"":           true,
	}

	if !validValues[statusFilter] {
		http.Error(w, "Invalid status value", http.StatusBadRequest)
		return
	}

	ctx := context.Background()
	var rows pgx.Rows
	var err error

	if statusFilter == "" {
		rows, err = s.dbPool.Query(ctx, `
			SELECT id, type, payload, status, retries, result, created_at, updated_at
			FROM tasks`)
	} else {
		rows, err = s.dbPool.Query(ctx, `
			SELECT id, type, payload, status, retries, result, created_at, updated_at
			FROM tasks WHERE status = $1`, statusFilter)
	}
	if err != nil {
		http.Error(w, "[API] Database error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	tasks := []model.Task{}
	for rows.Next() {
		var task model.Task
		err := rows.Scan(
			&task.ID, &task.Type, &task.Payload, &task.Status, &task.Retries,
			&task.Result, &task.CreatedAt, &task.UpdatedAt,
		)
		if err != nil {
			http.Error(w, "[API] Row scan error", http.StatusInternalServerError)
			return
		}
		tasks = append(tasks, task)
	}

	w.Header().Set("Content-Type", "application/json")
	if len(tasks) == 0 {
		if err := json.NewEncoder(w).Encode([]model.Task{}); err != nil {
			http.Error(w, "[API] Encoding error", http.StatusInternalServerError)
			return
		}
		return
	}

	if err := json.NewEncoder(w).Encode(tasks); err != nil {
		http.Error(w, "[API] Encoding error", http.StatusInternalServerError)
		return
	}
}

func (s *Server) cancelTaskByID(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		http.Error(w, "[API] Method Not Allowed", http.StatusMethodNotAllowed)
		return
	}

	idStr := r.PathValue("id")
	id, err := strconv.Atoi(idStr)
	if err != nil {
		http.Error(w, "[API] Invalid task ID", http.StatusBadRequest)
		return
	}

	var status string
	err = s.dbPool.QueryRow(context.Background(),
		`SELECT status FROM tasks WHERE id = $1`, id).Scan(&status)

	if err == pgx.ErrNoRows {
		http.Error(w, "[API] Task not found", http.StatusNotFound)
		return
	}
	if err != nil {
		http.Error(w, "[API] DB error", http.StatusInternalServerError)
		return
	}

	if status != "queued" {
		http.Error(w, "[API] Task cannot be cancelled from status: "+status, http.StatusConflict)
		return
	}

	if status == "queued" {
		err := queue.Remove(id)
		if err != nil {
			http.Error(w, "[API] Failed to remove from Redis queue", http.StatusInternalServerError)
			return
		}
	}

	cmdTag, err := s.dbPool.Exec(context.Background(),
		`UPDATE tasks SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = $1`, id)
	if err != nil {
		http.Error(w, "[API] Failed to cancel task", http.StatusInternalServerError)
		return
	}

	if cmdTag.RowsAffected() == 0 {
		http.Error(w, "[API] Nothing was updated", http.StatusConflict)
		return
	}

	w.WriteHeader(http.StatusNoContent)
}
