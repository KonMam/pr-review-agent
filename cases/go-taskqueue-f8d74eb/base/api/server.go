package api

import (
	"encoding/json"
	"go-taskqueue/model"
	"go-taskqueue/queue"
	"net/http"
	"strconv"
	"sync"
)


var taskStore = make(map[int]*model.Task)
var taskIDCounter int
var taskStoreMu sync.RWMutex


func getTask(w http.ResponseWriter, r *http.Request) {
	idStr := r.PathValue("id")
	id, err := strconv.Atoi(idStr)
	if err != nil {
		http.Error(w, "Invalid task ID", http.StatusBadRequest)
		return
	}

	taskStoreMu.RLock()
	task, ok := taskStore[id]
	taskStoreMu.RUnlock()

	if !ok {
		http.Error(w, "task not found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(task)
}

func postTask(w http.ResponseWriter, r *http.Request) {
	var task model.Task
	err := json.NewDecoder(r.Body).Decode(&task)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}

	taskStoreMu.Lock()
	taskIDCounter++
	task.ID = taskIDCounter
	task.Status = "queued"
	taskStore[task.ID] = &task
	taskStoreMu.Unlock()
	
	queue.Tasks <- task

	w.WriteHeader(http.StatusCreated)
	json.NewEncoder(w).Encode(task)
}

func NewServer(addr string) *http.Server {
	mux := http.NewServeMux()

	mux.HandleFunc("GET /tasks/{id}", getTask)
	mux.HandleFunc("POST /tasks", postTask)

	return &http.Server{
		Addr:    addr,
		Handler: mux,
	}
}
